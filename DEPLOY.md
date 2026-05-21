# Finance Tracker — Deploy Guide

## Local Development

```bash
# 1. Create virtualenv and install
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Copy and fill in env vars
cp .env.example .env
# Edit .env — set DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID
# Set LOCAL_POLLING=true

# 3a. Run the dashboard (auto-reloads on save)
uvicorn app.dashboard:app --reload
# → http://localhost:8000

# 3b. In a separate terminal, run the bot in polling mode
python run_polling.py
```

SQLite is used automatically when DATABASE_URL is not set.

---

## Supabase Setup

1. Go to Supabase → Settings → Database → URI
2. Copy the **psycopg2** connection string (starts with `postgresql://`)
3. Set `DATABASE_URL=` in your `.env` / Render env vars

The app runs `ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_impulse ...`
on first start — no manual migration needed.

---

## Render Deployment

1. Push the project to a GitHub repo
2. Create a **Web Service** on [render.com](https://render.com)
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.dashboard:app --host 0.0.0.0 --port $PORT`
   - (or just use the Procfile — Render picks it up automatically)
3. Add all environment variables from `.env.example`
   - Set `LOCAL_POLLING=false`
   - Set `WEBHOOK_BASE_URL=https://<your-app>.onrender.com`

### Register the Telegram webhook (run once after deploy)

```bash
curl -X POST \
  "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<RENDER_URL>/webhook&secret_token=<SECRET>"
```

---

## Prevent Render free-tier spin-down

The free tier spins down after 15 min of inactivity, making the bot slow on first message.

**Fix:** Use [UptimeRobot](https://uptimerobot.com) (free) to ping `/ping` every 5 minutes:

- Monitor type: HTTP(s)
- URL: `https://<your-app>.onrender.com/ping`
- Interval: 5 minutes

---

## Bot Commands

| Command    | Description                        |
|------------|------------------------------------|
| `/start`   | Welcome + usage examples           |
| `/help`    | Full command list                  |
| `/today`   | Today's transactions               |
| `/week`    | This week's summary + budget bar   |
| `/month`   | This month's breakdown             |
| `/income`  | Monthly income total               |
| `/budget`  | Weekly budget status               |
| `/undo`    | Undo the last transaction          |
| `/summary` | Net worth + cash flow snapshot     |

**Logging a transaction** — just type naturally:

```
14 kebab                 → expense, Food
spent 40 groceries       → expense, Food
+2400 salary             → income, Income
100 to investments       → transfer, To Investments
40 shoes impulse         → expense, Clothing, is_impulse=true
```

After each expense the bot replies with a weekly budget progress bar.
