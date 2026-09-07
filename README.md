# Relay — Telegram Automation Hub

Open-source personal automation hub for Telegram bots backed by **Google Sheets** and **Google Gemini**.

Run a local web UI to start/stop automations. Each automation is its own Telegram bot with its own spreadsheet (and optional worksheet tab).

## Features

| Automation | What it does |
|------------|----------------|
| **Pantry Inventory** | Add grocery items from text or photos, store them in Sheets, fuzzy `/search`, inline count ±1 / delete |
| **Medicine Inventory** | Track medicines/supplements; `/search` matches by **brand, formula, or symptoms** (e.g. `headache` → paracetamol) via Gemini |

## Architecture

```text
main.py                 → starts the Relay web UI
app/registry.py         → registers automations
app/web/                → FastAPI UI (list + Start/Stop)
automations/base.py     → shared Automation contract
automations/pantry/     → pantry Telegram bot
automations/medicine/   → medicine Telegram bot
```

Add a new bot by implementing `Automation` under `automations/` and registering it in `app/registry.py`.

## Requirements

- Python 3.11+
- Telegram bots from [@BotFather](https://t.me/BotFather)
- [Google AI Studio](https://aistudio.google.com/) Gemini API key
- Google Cloud **service account** JSON with access to your spreadsheet(s)

## Quick start

```bash
git clone https://github.com/mohdahmedasif/telegram-automation.git
cd telegram-automation
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # or: cp .env.example .env
```

1. Place your service-account key at `credentials.json` (or set `CREDENTIALS_PATH`).
2. Share each Google Sheet with the service account `client_email` (Editor).
3. Fill `.env` (see below).
4. Start the hub:

```bash
python main.py
```

Open **http://127.0.0.1:8765**, then **Start** an automation.

## Configuration

Copy `.env.example` → `.env`. Each automation has its **own** bot token and spreadsheet ID. Optional `*_WORKSHEET_GID` selects a tab inside that spreadsheet (the `gid=` value from the Sheets URL).

```env
GEMINI_API_KEY=...
CREDENTIALS_PATH=credentials.json

PANTRY_TELEGRAM_BOT_TOKEN=...
PANTRY_SPREADSHEET_ID=...
# PANTRY_WORKSHEET_GID=...

MEDICINE_TELEGRAM_BOT_TOKEN=...
MEDICINE_SPREADSHEET_ID=...
# MEDICINE_WORKSHEET_GID=...
```

Never commit `.env` or `credentials.json` — they are gitignored.

## Telegram commands

### Pantry

| Command | Description |
|---------|-------------|
| `/add <item>` | Add an item (photos work too) |
| `/search <query>` | Fuzzy find items |
| `/edit <query>` | Adjust count or delete |
| `/list` | Recent items |
| `/cancel` | Cancel an in-progress add |

### Medicine

| Command | Description |
|---------|-------------|
| `/add <name>` | Add a medicine/supplement |
| `/search <name\|formula\|symptom>` | e.g. `/search headache` or `/search pantoprazole` |
| `/edit <query>` | Adjust count or delete |
| `/list` | Recent items |
| `/cancel` | Cancel an in-progress add |

## Google Sheet schemas

**Pantry:** Item Name, Category, Storage Location, Count, Container Type, Unit Size, Reorder Status, Expiration Date, Notes  

**Medicine:** Item Name, Formula, Storage Location, Type, Count, Container Type, Count per Units, Unit Size, Reorder Status, Expiration Date, Notes  

Row 1 = headers; data starts at row 2.

## Adding another automation

1. Create `automations/your_bot/` with an `Automation` subclass.
2. Register it in `app/registry.py`.
3. Document its `YOURBOT_*` env vars in `.env.example`.

It will appear automatically in the Relay UI.

## Auto-deploy (GitHub Actions on your VPS)

On every push to `main`, GitHub runs [`scripts/deploy.sh`](scripts/deploy.sh) **on your VPS** via a self-hosted runner (git pull → pip install → restart).

No SSH deploy key is required — the job already runs on the server.

### 1. One-time: install the app on the VPS

```bash
git clone https://github.com/mohdahmedasif/telegram-automation.git
cd telegram-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill secrets + credentials.json

sudo cp deploy/relay.service /etc/systemd/system/relay.service
# edit User= / paths in that file
sudo systemctl daemon-reload
sudo systemctl enable --now relay
```

### 2. One-time: add a self-hosted runner (on the VPS)

In GitHub: **Settings → Actions → Runners → New self-hosted runner**  
Pick **Linux**, then run the commands GitHub shows on the VPS (download, `./config.sh`, `./run.sh`).

For a background service after config:

```bash
sudo ./svc.sh install
sudo ./svc.sh start
```

Optional secret: `DEPLOY_PATH` = full path to the repo on the VPS  
(e.g. `/home/ubuntu/telegram-automation`). If unset, the runner looks in `$HOME/telegram-automation`.

### 3. Deploy

Push to `main`, or **Actions → Deploy → Run workflow**.

## License

MIT — see [LICENSE](LICENSE).
