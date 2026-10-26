# insta-alert-bot

Bot Telegram che, per ogni profilo Instagram in una lista, manda un messaggio
**"Hey, new post here: <url>"** quando esce un post nuovo.

## Setup locale

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # poi metti TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID
.venv/bin/python bot.py --once   # un giro di prova
```

Le URL da controllare stanno in `INSTAGRAM_URLS` (virgole o newline) oppure in
`urls.txt`, una per riga. Al primo avvio il bot salva una *baseline* dei post
esistenti e non manda nulla: da li' in poi notifica solo i post nuovi.
Lo stato vive in `state.json` (cancellalo per ripartire da zero).

## Dove arrivano i messaggi

Il bot puo' scrivere **in privato** (DM) oppure in un gruppo/canale, cambia solo
`TELEGRAM_CHAT_ID`:

- **DM dal bot** (consigliato): metti il tuo user id. Telegram non permette a un
  bot di iniziare una conversazione, quindi prima apri la chat con il tuo bot e
  premi **Start** (o mandagli un messaggio qualsiasi).
- **Gruppo**: aggiungi il bot al gruppo e usa l'id negativo del gruppo
  (es. `-1001234567890`).
- **Canale**: bot admin del canale, id negativo del canale; con
  `disable_web_page_preview=false` l'anteprima del post viene mostrata.

Per trovare l'id giusto, dopo aver scritto al bot:

```bash
.venv/bin/python bot.py --chat-id
# TELEGRAM_CHAT_ID=123456789    # Giulio [private]
```

All'avvio il bot logga tipo e nome della chat di destinazione (es. "chat privata
con Giulio: i messaggi arrivano in DM dal bot"), cosi' sai subito se hai
configurato un DM o un gruppo.

## Autenticazione Instagram (necessaria)

Instagram non serve piu' i dati dei profili alle richieste anonime: senza login
`web_profile_info` risponde `401 require_login` oppure `429` (limite per IP),
mentre la pagina HTML non contiene i post. Quindi il bot deve usare i cookie di
una sessione loggata.

### Cosa e' stato testato (26/10/2026): tutte le vie "senza account" sono morte

| via senza account | esito |
|---|---|
| `web_profile_info` / `graphql/query` anonimi | `401 {"require_login":true}` / `429` |
| HTML del profilo, `/<utente>/embed/` | 200 ma **senza post** (caricati via JS) |
| RSSHub pubblici (rsshub.app, rssforever, ktachibana...) | 403 / 503 / timeout |
| RSS-Bridge pubblico (suumitsu.eu) | 400 |
| viewer mirror (pixwox, greatfon, dumpor, imginn) | 403 / pagine vuote via JS |

Instagram inoltre non ha un'API pubblica per i profili altrui: la Graph API
ufficiale funziona solo se il profilo e' tuo e di tipo business. Per leggere un
profilo pubblico serve comunque un login, come per qualsiasi browser umano.

### Se non hai un account Instagram: creane uno "usa e getta"

Non devi usarlo come persona: serve solo al bot per guardare i profili che ti
interessano, e non pubblichera' mai nulla. Cinque minuti:

1. Da desktop, in **finestra privata**: `instagram.com` → Registrati. Basta un
   indirizzo email (da PC di solito non chiede il telefono); username qualsiasi.
2. Resta loggato: **non fare logout**, perche' il logout invalida il cookie
   `sessionid` che stiamo per copiare.
3. DevTools (F12) → tab **Network** → ricarica la pagina → clicca una richiesta
   verso `www.instagram.com` → **Request Headers** → copia il valore di
   **Cookie** (deve contenere almeno `sessionid`, `csrftoken`, `ds_user_id`).
4. Incollalo in `.env`:

   ```
   INSTAGRAM_COOKIES=sessionid=...; csrftoken=...; ds_user_id=...; mid=...
   ```

5. Verifica senza notificare ne' sporcare lo stato:

   ```bash
   .venv/bin/python bot.py --check
   ```

   Stampa i 3 post piu' recenti per profilo con l'eta', oppure l'errore esatto
   (`401`, `429`, profilo privato...).

I cookie durano settimane/mesi; quando scadono il bot logga `401` e basta
ripetere il copia-incolla (nuova finestra privata, nuovo cookie).

Rischi: minimi. Il bot fa una richiesta ogni `POLL_INTERVAL` (default 15 min):
e' il volume tipico dei bot di notifiche open source dal 2023, quando Instagram
ha tolto la navigazione anonima. Se anche cosi' Instagram reagisce (raro),
alza `POLL_INTERVAL` a 1200-1800 secondi.

Alternativa senza browser: `INSTAGRAM_USERNAME` + `INSTAGRAM_PASSWORD` con
login automatico via `instaloader`. Non implementata perche' il login da IP
datacenter (la VPS) puo' attivare la verifica del telefono; i cookie di una
sessione creata da casa restano la via piu' robusta.

## Deploy sulla VPS (systemd)

```bash
sudo useradd --system --home /opt/insta-alert-bot --shell /usr/sbin/nologin instabot
sudo mkdir -p /opt/insta-alert-bot
sudo rsync -a --exclude .venv --exclude .git ./ /opt/insta-alert-bot/
cd /opt/insta-alert-bot
sudo -u instabot python3 -m venv .venv
sudo -u instabot .venv/bin/pip install -r requirements.txt
# crea /opt/insta-alert-bot/.env con il token e i permessi giusti:
sudo install -o instabot -g instabot -m 600 .env.example /opt/insta-alert-bot/.env
# poi edita il file con i valori veri

sudo cp deploy/insta-alert-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insta-alert-bot
```

Log e stato:

```bash
journalctl -u insta-alert-bot -f
sudo -u instabot cat /opt/insta-alert-bot/state.json
```

## Note

- `POLL_INTERVAL` minimo 60s, default 900s (15 min): sotto i ~10 minuti
  Instagram inizia a rispondere 429.
- Se il bot resta spento a lungo, notifica al massimo gli ultimi
  `MAX_NOTIFICATIONS_PER_POLL` (5) post per profilo per non floodare la chat.
