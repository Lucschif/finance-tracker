#!/usr/bin/env python3
"""Local development: run the Telegram bot in polling mode.

Usage:
    python run_polling.py

In a separate terminal, run the dashboard with:
    uvicorn app.dashboard:app --reload

Set LOCAL_POLLING=true in your .env so the dashboard does not
attempt to register a webhook when it starts.
"""
import logging

from app.database import init_db
from app.bot import create_ptb_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

if __name__ == "__main__":
    init_db()
    ptb = create_ptb_app()
    print("Bot started in polling mode. Press Ctrl+C to stop.")
    ptb.run_polling()
