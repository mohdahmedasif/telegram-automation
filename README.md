# Relay — Telegram Automation Hub

Open-source personal automation hub for Telegram bots backed by **Google Sheets** and **Google Gemini**.

Run a local web UI to start/stop automations. Each automation is its own Telegram bot with its own spreadsheet (and optional worksheet tab).

## Features

| Automation | What it does |
|------------|----------------|
| **Household Inventory** | One bot for pantry groceries and medicine/supplements in a single Google Sheet. Add items from text or photos — Gemini figures out the category; `/search` matches by name, brand, category, or (for medicine) symptoms (e.g. `headache` → paracetamol). Adding is conversational: describe the item, confirm a one-message recap, correct anything by just typing — no step-by-step wizard. |

## Architecture

```text
main.py                    → starts the Relay web UI
app/registry.py            → registers automations
app/web/                   → FastAPI UI (list + Start/Stop)
automations/base.py        → shared Automation contract
automations/inventory_sheet.py → shared sheet schema + gspread I/O
automations/inventory/     → household inventory Telegram bot
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

Copy `.env.example` → `.env`. Optional `INVENTORY_WORKSHEET_GID` selects a tab inside the spreadsheet (the `gid=` value from the Sheets URL).

```env
GEMINI_API_KEY=...
CREDENTIALS_PATH=credentials.json

INVENTORY_TELEGRAM_BOT_TOKEN=...
INVENTORY_SPREADSHEET_ID=...
# INVENTORY_WORKSHEET_GID=...
```

Never commit `.env` or `credentials.json` — they are gitignored.

## Telegram commands

| Command | Description |
|---------|-------------|
| `/add <item>` | Add an item — text or photo. Also works without the command: just describe what you got and the bot starts the same conversation. |
| `/search <name\|brand\|category\|symptom>` | e.g. `/search pasta`, `/search headache`, `/search nexpro` |
| `/edit <query>` | Adjust count or delete |
| `/list` | Recent items |
| `/cancel` | Cancel an in-progress add |

Adding is a short conversation, not a fixed wizard: describe the item (or send a photo), the bot fills in everything it can and asks only if the name itself is unclear, then shows a one-message recap. Reply with any correction in plain English (e.g. "actually 3 packs, put it in the basement") or tap **Save**/**Cancel**.

## Google Sheet schema

One shared tab covers both groceries and medicine/supplements via the `category` column:

`item_id, name, brand, category, location, package_type, package_count, units_per_package, size_value, size_unit, expiry_date, notes, last_updated`

- `category` dropdown: `Grains & Rice, Canned Goods, Pasta & Noodles, Seasonings & Spices, Beverages, Medicine, Supplement`
- `location` dropdown: `Sofa Storage, Kitchen Cabinet, Basement, Washroom Cabinet`
- `package_type` dropdown: `Tablet Strip, Bottle, Flask, Box, Sachet, Jar, Can, Pack, Loose`
- `item_id` and `last_updated` are bot-assigned (sequential id, today's date) — everything else comes from Gemini extraction or your corrections.
- For medicine, symptom/purpose text lives in `notes`; `/search` matches on `name`, `brand`, `category`, and `notes`, so a query like `headache` or `pantoprazole` still finds the right row.
- Optional formula columns (days-until-expiry, reorder status) can live in the sheet; the bot never writes them.

Row 1 = headers; data starts at row 2.

## Adding another automation

1. Create `automations/your_bot/` with an `Automation` subclass.
2. Register it in `app/registry.py`.
3. Document its `YOURBOT_*` env vars in `.env.example`.

It will appear automatically in the Relay UI.

## Auto-deploy (GitHub Actions → VPS over SSH)

On every push to `main`, GitHub SSHs into your VPS and runs [`scripts/deploy.sh`](scripts/deploy.sh)
(git pull → pip install → restart).

### 1. One-time: app on the VPS

```bash
git clone https://github.com/mohdahmedasif/telegram-automation.git
cd telegram-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill secrets + credentials.json

sudo cp deploy/relay.service /etc/systemd/system/relay.service
# edit User= / paths
sudo systemctl daemon-reload
sudo systemctl enable --now relay
```

Make sure your deploy public key is in `~/.ssh/authorized_keys` on the VPS.

### 2. GitHub secrets

| Secret | Meaning |
|--------|---------|
| `DEPLOY_HOST` | VPS IP / hostname |
| `DEPLOY_USER` | SSH user |
| `DEPLOY_PATH` | Full path to the repo on the VPS |
| `DEPLOY_SSH_KEY` | Private key (**set from file**, see below) |
| `DEPLOY_PORT` | Optional (default 22) |

```powershell
# Windows — set key from file (no paste)
Get-Content -Raw $env:USERPROFILE\.ssh\github-actions | gh secret set DEPLOY_SSH_KEY --repo mohdahmedasif/telegram-automation
```

### 3. Deploy

Push to `main`, or **Actions → Deploy → Run workflow**.

Manual on the server:

```bash
bash scripts/deploy.sh
```

## License

MIT — see [LICENSE](LICENSE).
