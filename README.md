# Grok desk — private FOMO trading desk (PAPER first)

One Python 3.12 process: 5 asyncio agents (Scanner → Auditor → Analyst → Executor → Risk),
FastAPI + SSE, an iPhone PWA (`web/`) with Web Push. **It never places orders, never asks for the
FOMO password, never touches funds.** LIVE mode only sends BUY/SELL signals with FOMO links.

## Install on the droplet (DigitalOcean Web Console, as root)
    curl -fsSL <BUNDLE_URL> -o gd.sh && bash gd.sh
Then `grok-desk-url` prints your private app link (`https://<ip-with-dashes>.sslip.io/#t=<code>`).
Open it in Safari → Share → Add to Home Screen → open Desk from the icon → paste the code if asked
→ Account → Push alerts.

## Layout
    main.py config.py fields.py db.py bus.py links.py fees.py sources.py notify.py commands.py
    agents/{scanner,auditor,analyst,executor,risk,base}.py  telegram.py webpush.py api.py report.py
    web/ (index.html app.css app.js sw.js manifest)  tests/  deploy/  tools/  logs/raw/

## Tests
    python -m pytest -q tests

## Secrets
Only in `/etc/grok-desk/desk.env` (root, 600) on the server: APP_TOKEN, VAPID keys, and empty
TELEGRAM_/JEV_ slots. Never in the repo, web/, logs or API responses (tests/test_secrets.py).
