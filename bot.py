#!/usr/bin/env python3
"""insta-alert-bot

Controlla periodicamente una lista di profili Instagram e manda un messaggio
Telegram "Hey, new post here: <url>" per ogni nuovo post pubblicato.

Configurazione tramite .env (vedi .env.example):

    TELEGRAM_BOT_TOKEN   token del bot (obbligatorio)
    TELEGRAM_CHAT_ID     chat/gruppo/canale dove mandare gli avvisi (obbligatorio)
    INSTAGRAM_URLS       url separati da virgola o newline
    POLL_INTERVAL        secondi tra un controllo e l'altro (default 900)
    STATE_FILE           dove salvare i post gia' visti (default state.json)
    FIRST_RUN_NOTIFY     true per notificare l'ultimo post al primo avvio
    INSTAGRAM_COOKIES    header Cookie del browser (obbligatorio in pratica:
                         Instagram serve i dati dei profili solo a chi e' loggato)
    INSTAGRAM_SESSIONID  solo il cookie sessionid, alternativa piu' snella

Uso:
    python bot.py            # loop infinito (come da .service)
    python bot.py --check    # legge i profili e stampa gli ultimi post, senza notificare
    python bot.py --once     # un solo giro, utile per test
    python bot.py --chat-id  # stampa i chat id visti dal bot (per il .env)
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_URL = "https://www.instagram.com/p/{shortcode}/"
PROFILE_API = "https://www.instagram.com/api/v1/users/web_profile_info/"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# tipo di chat restituito da getChat, per distinguere DM da gruppi/canali
PRIVATE_CHAT = "private"

# App id pubblico del sito web di Instagram: serve per l'endpoint JSON.
IG_APP_ID = "936619743392459"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_SEEN_PER_PROFILE = 200       # quanti shortcode ricordare per profilo
MAX_NOTIFICATIONS_PER_POLL = 5   # anti-flood se il bot e' stato spento a lungo
HTTP_TIMEOUT = 20

RESERVED_PATHS = {"p", "reel", "reels", "tv", "explore", "stories", "accounts"}

log = logging.getLogger("insta-alert-bot")


# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: str
    urls: Tuple[str, ...]
    poll_interval: int
    state_file: Path
    first_run_notify: bool
    instagram_sessionid: Optional[str]
    instagram_cookies: Optional[str] = None


def _bool(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_urls(raw: str) -> List[str]:
    chunks = raw.replace(",", "\n").splitlines()
    return [c.strip() for c in chunks if c.strip()]


def load_config(require_urls: bool = True, require_chat_id: bool = True) -> Config:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN mancante nel .env")
    if not chat_id and require_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID mancante nel .env")

    urls = _split_urls(os.getenv("INSTAGRAM_URLS") or "")
    if not urls:
        # fallback: file urls.txt accanto al bot, una url per riga
        urls_file = BASE_DIR / "urls.txt"
        if urls_file.is_file():
            urls = _split_urls(urls_file.read_text(encoding="utf-8"))
    if not urls and require_urls:
        raise SystemExit(
            "Nessuna url da controllare: imposta INSTAGRAM_URLS o crea urls.txt"
        )

    try:
        poll_interval = int(os.getenv("POLL_INTERVAL", "900"))
    except ValueError:
        raise SystemExit("POLL_INTERVAL deve essere un intero (secondi)")
    poll_interval = max(poll_interval, 60)

    state_file = Path(os.getenv("STATE_FILE", "state.json")).expanduser()
    if not state_file.is_absolute():
        state_file = BASE_DIR / state_file

    return Config(
        token=token,
        chat_id=chat_id,
        urls=tuple(urls),
        poll_interval=poll_interval,
        state_file=state_file,
        first_run_notify=_bool(os.getenv("FIRST_RUN_NOTIFY")),
        instagram_sessionid=(os.getenv("INSTAGRAM_SESSIONID") or "").strip() or None,
        instagram_cookies=(os.getenv("INSTAGRAM_COOKIES") or "").strip() or None,
    )


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #


class FetchError(RuntimeError):
    """Errore non recuperabile nel recupero dei post di un profilo."""


def username_from_url(url: str) -> str:
    """Estrae lo username da un url Instagram (profilo, /p/, /reel/, ...)."""
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("@")
    parsed = urlparse(raw)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise FetchError("url Instagram non valido: %s" % url)
    if parts[0].lower() in RESERVED_PATHS:
        raise FetchError(
            "l'url deve puntare a un profilo, non a un singolo post: %s" % url
        )
    return parts[0].lstrip("@")


def _parse_cookie_header(raw: str) -> Dict[str, str]:
    """Trasforma "k=v; k2=v2" (header Cookie del browser) in dict."""
    cookies: Dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def build_instagram_session(cfg: Config) -> requests.Session:
    """Sessione HTTP per Instagram, autenticata se ci sono i cookie."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-ig-app-id": IG_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        }
    )

    cookies: Dict[str, str] = {}
    if cfg.instagram_cookies:
        cookies.update(_parse_cookie_header(cfg.instagram_cookies))
    if cfg.instagram_sessionid:
        cookies.setdefault("sessionid", cfg.instagram_sessionid)

    for name, value in cookies.items():
        if value:
            session.cookies.set(name, value, domain=".instagram.com")
    if cookies.get("csrftoken"):
        session.headers["x-csrftoken"] = cookies["csrftoken"]

    log.info(
        "sessione Instagram: %s",
        "autenticata (cookie: %s)" % ", ".join(sorted(k for k, v in cookies.items() if v))
        if cookies
        else "anonima (Instagram oggi richiede il login: vedi README)",
    )
    return session


def _backoff(attempt: int) -> None:
    # backoff esponenziale con jitter, per non martellare Instagram
    time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1.5))


def fetch_posts(
    session: requests.Session, username: str, attempts: int = 3
) -> List[Dict[str, Any]]:
    """Ritorna i post del profilo, dal piu' vecchio al piu' recente.

    Ogni post e' un dict con "shortcode", "timestamp" e "url".
    """
    params = {"username": username}
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(PROFILE_API, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_error = exc
            log.warning(
                "errore di rete su @%s (tentativo %d): %s", username, attempt, exc
            )
            if attempt < attempts:
                _backoff(attempt)
            continue

        if resp.status_code in (401, 403):
            raise FetchError(
                "Instagram richiede il login per @%s (HTTP %d): incolla in "
                "INSTAGRAM_COOKIES l'header Cookie del browser (DevTools -> "
                "Network -> una richiesta su instagram.com -> Request Headers)."
                % (username, resp.status_code)
            )
        if resp.status_code == 429:
            last_error = FetchError(
                "rate limit (429) su @%s: Instagram limita le sessioni anonime, "
                "imposta INSTAGRAM_COOKIES nel .env" % username
            )
            wait = resp.headers.get("Retry-After")
            delay = int(wait) if wait and wait.isdigit() else min(60 * attempt, 300)
            if attempt < attempts:
                log.warning("rate limit su @%s, attendo %ds", username, delay)
                time.sleep(delay)
            continue
        if resp.status_code == 404:
            raise FetchError("profilo @%s inesistente (404)" % username)
        if resp.status_code != 200:
            last_error = FetchError(
                "risposta inattesa %d su @%s" % (resp.status_code, username)
            )
            if attempt < attempts:
                _backoff(attempt)
            continue

        try:
            payload = resp.json()
        except ValueError as exc:
            last_error = FetchError("risposta non JSON su @%s: %s" % (username, exc))
            if attempt < attempts:
                _backoff(attempt)
            continue

        user = (payload.get("data") or {}).get("user")
        if not user:
            raise FetchError(
                "profilo @%s non accessibile (privato, inesistente o bloccato)"
                % username
            )

        edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
        posts: List[Dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node") or {}
            shortcode = node.get("shortcode")
            if not shortcode:
                continue
            posts.append(
                {
                    "shortcode": shortcode,
                    "timestamp": int(node.get("taken_at_timestamp") or 0),
                    "url": PROJECT_URL.format(shortcode=shortcode),
                }
            )
        posts.sort(key=lambda p: p["timestamp"])
        return posts

    raise FetchError("impossibile leggere @%s: %s" % (username, last_error))


# --------------------------------------------------------------------------- #
# Stato persistente
# --------------------------------------------------------------------------- #


class State:
    """Shortcode gia' notificati, per profilo, salvati su file JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, List[str]] = self._load()

    def _load(self) -> Dict[str, List[str]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            log.warning("state file illeggibile (%s), salvo copia in %s", exc, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return {}
        if not isinstance(raw, dict):
            log.warning("state file con formato inatteso, riparto da zero")
            return {}
        return {
            str(k): [str(s) for s in v] for k, v in raw.items() if isinstance(v, list)
        }

    def seen(self, username: str) -> Set[str]:
        return set(self.data.get(username, []))

    def mark_seen(self, username: str, shortcodes: Sequence[str]) -> None:
        merged = list(dict.fromkeys(list(shortcodes) + self.data.get(username, [])))
        self.data[username] = merged[:MAX_SEEN_PER_PROFILE]
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


class TelegramError(RuntimeError):
    """Chiamata alle Bot API fallita dopo i tentativi disponibili."""


def telegram_call(
    cfg: Config,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    attempts: int = 3,
) -> Dict[str, Any]:
    """Chiama una Bot API. GET se non c'e' payload, altrimenti POST JSON."""
    url = TELEGRAM_API.format(token=cfg.token, method=method)
    last_error: Optional[str] = None

    for attempt in range(1, attempts + 1):
        try:
            if payload is None:
                resp = requests.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
            else:
                resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("%s: errore di rete (tentativo %d): %s", method, attempt, exc)
            if attempt < attempts:
                _backoff(attempt)
            continue

        if resp.status_code == 200:
            return resp.json().get("result") or {}

        if resp.status_code == 429:
            retry_after = 5
            try:
                retry_after = int(
                    (resp.json().get("parameters") or {}).get("retry_after", 5)
                )
            except (ValueError, AttributeError):
                pass
            log.warning("Telegram rate limit su %s, attendo %ds", method, retry_after)
            if attempt < attempts:
                time.sleep(retry_after)
            continue

        if resp.status_code in (400, 401, 403, 404):
            # token/chat_id sbagliati: inutile ritentare, meglio fermarsi
            raise SystemExit(
                "Telegram ha rifiutato %s (%d): %s"
                % (method, resp.status_code, resp.text[:300])
            )

        last_error = "HTTP %d: %s" % (resp.status_code, resp.text[:300])
        log.warning("Telegram ha risposto a %s: %s", method, last_error)
        if attempt < attempts:
            _backoff(attempt)

    raise TelegramError("%s fallita: %s" % (method, last_error))


def send_message(cfg: Config, text: str) -> bool:
    payload = {"chat_id": cfg.chat_id, "text": text, "disable_web_page_preview": False}
    try:
        telegram_call(cfg, "sendMessage", payload=payload)
        return True
    except TelegramError as exc:
        log.error("messaggio non inviato: %s", exc)
        return False


def describe_target(cfg: Config) -> None:
    """Logga chi e' il bot e che tipo di chat e' TELEGRAM_CHAT_ID."""
    try:
        me = telegram_call(cfg, "getMe")
        chat = telegram_call(cfg, "getChat", params={"chat_id": cfg.chat_id})
    except TelegramError as exc:
        log.warning("controllo Telegram non riuscito: %s", exc)
        return

    chat_type = chat.get("type", "?")
    if chat_type == PRIVATE_CHAT:
        name = " ".join(
            p for p in (chat.get("first_name"), chat.get("last_name")) if p
        ) or chat.get("username", "")
        log.info(
            "bot @%s -> chat privata con %s (id %s): i messaggi arrivano in DM dal bot",
            me.get("username", "?"),
            name or "utente",
            cfg.chat_id,
        )
    else:
        log.info(
            "bot @%s -> chat di tipo '%s' (id %s): i messaggi arrivano li', "
            "non in privato. Per il DM usa il tuo user id come TELEGRAM_CHAT_ID.",
            me.get("username", "?"),
            chat_type,
            cfg.chat_id,
        )


def print_chat_ids(cfg: Config) -> int:
    """Stampa i chat id degli ultimi messaggi ricevuti dal bot (getUpdates)."""
    try:
        updates = telegram_call(
            cfg,
            "getUpdates",
            params={"limit": 100, "allowed_updates": '["message"]'},
        )
    except TelegramError as exc:
        log.error("%s", exc)
        return 1

    chat_ids: Dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is None:
            continue
        label = chat.get("username") or " ".join(
            p for p in (chat.get("first_name"), chat.get("title")) if p
        )
        chat_ids[str(chat["id"])] = "%s [%s]" % (label or "?", chat.get("type", "?"))

    if not chat_ids:
        print(
            "Nessun update disponibile. Apri la chat con il bot, premi Start o "
            "mandagli un messaggio, poi rilancia: python bot.py --chat-id"
        )
        return 0

    for cid, label in chat_ids.items():
        print("TELEGRAM_CHAT_ID=%s    # %s" % (cid, label))
    return 0


# --------------------------------------------------------------------------- #
# Logica principale
# --------------------------------------------------------------------------- #


def check_profile(
    cfg: Config,
    session: requests.Session,
    state: State,
    url: str,
) -> int:
    """Controlla un profilo e notifica i nuovi post. Ritorna quanti avvisi manda."""
    username = username_from_url(url)
    posts = fetch_posts(session, username)
    if not posts:
        log.info("@%s: nessun post visibile", username)
        return 0

    seen = state.seen(username)

    if not seen:
        # primo avvio: registra lo stato attuale senza spammare la cronologia
        new = list(posts[-1:]) if cfg.first_run_notify else []
        state.mark_seen(username, [p["shortcode"] for p in posts])
        log.info(
            "@%s: baseline iniziale con %d post (notifiche: %d)",
            username,
            len(posts),
            len(new),
        )
    else:
        new = [p for p in posts if p["shortcode"] not in seen]
        # segna tutto come visto, anche cio' che eventualmente non notifichiamo
        state.mark_seen(username, [p["shortcode"] for p in new])
        log.info("@%s: %d nuovi post", username, len(new))

    if not new:
        return 0

    if len(new) > MAX_NOTIFICATIONS_PER_POLL:
        skipped = len(new) - MAX_NOTIFICATIONS_PER_POLL
        log.warning(
            "@%s: %d nuovi post, notifico solo i %d piu' recenti (salto %d)",
            username,
            len(new),
            MAX_NOTIFICATIONS_PER_POLL,
            skipped,
        )
        new = new[-MAX_NOTIFICATIONS_PER_POLL:]

    sent = 0
    for post in new:
        text = "Hey, new post here: %s" % post["url"]
        if send_message(cfg, text):
            sent += 1
            log.info("@%s: notificato %s", username, post["url"])
        time.sleep(1)  # non floodare Telegram
    return sent


def run_once(cfg: Config, session: requests.Session, state: State) -> int:
    sent = 0
    for index, url in enumerate(cfg.urls):
        if index:
            time.sleep(random.uniform(2, 5))  # sparpaglia le richieste
        try:
            sent += check_profile(cfg, session, state, url)
        except FetchError as exc:
            log.error("%s", exc)
        except Exception:  # noqa: BLE001 - il loop non deve morire
            log.exception("errore inatteso su %s", url)
    return sent


def diagnose(cfg: Config, session: requests.Session) -> int:
    """Legge i profili senza toccare Telegram ne' lo state (per il setup)."""
    failures = 0
    for url in cfg.urls:
        try:
            username = username_from_url(url)
            posts = fetch_posts(session, username, attempts=1)
        except FetchError as exc:
            failures += 1
            log.error("%s", exc)
            continue

        log.info("@%s: %d post visibili", username, len(posts))
        for post in posts[-3:]:
            log.info("   %s (%s)", post["url"], _ago(post["timestamp"]))

    return 1 if failures else 0


def _ago(timestamp: int) -> str:
    if not timestamp:
        return "data sconosciuta"
    delta = max(int(time.time()) - timestamp, 0)
    if delta < 3600:
        return "%d min fa" % (delta // 60)
    if delta < 86400:
        return "%d h fa" % (delta // 3600)
    return "%d giorni fa" % (delta // 86400)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv(BASE_DIR / ".env")

    logging.basicConfig(
        level=logging.DEBUG if _bool(os.getenv("DEBUG")) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    chat_id_mode = "--chat-id" in args
    check_mode = "--check" in args
    cfg = load_config(
        require_urls=not chat_id_mode,
        require_chat_id=not (chat_id_mode or check_mode),
    )

    if chat_id_mode:
        return print_chat_ids(cfg)

    log.info(
        "avvio: %d profili, intervallo %ds, state %s",
        len(cfg.urls),
        cfg.poll_interval,
        cfg.state_file,
    )

    session = build_instagram_session(cfg)

    if check_mode:
        return diagnose(cfg, session)

    state = State(cfg.state_file)

    describe_target(cfg)

    if "--once" in args:
        sent = run_once(cfg, session, state)
        log.info("giro singolo completato, %d notifiche", sent)
        return 0

    stop = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal stop
        log.info("ricevuto segnale %d, esco", signum)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop:
        run_once(cfg, session, state)
        # sleep a piccoli passi per reagire subito a SIGTERM
        for _ in range(cfg.poll_interval):
            if stop:
                break
            time.sleep(1)

    log.info("fermo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
