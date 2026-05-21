import os
from dotenv import load_dotenv

load_dotenv()

_raw_db = os.getenv("DATABASE_URL") or "sqlite:///./finance.db"
DATABASE_URL: str = _raw_db.replace("postgres://", "postgresql://", 1)

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_OWNER_CHAT_ID: int = int(os.getenv("TELEGRAM_OWNER_CHAT_ID") or "0")
TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
WEBHOOK_BASE_URL: str = os.getenv("WEBHOOK_BASE_URL") or ""
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY") or ""
WEEKLY_BUDGET: float = float(os.getenv("WEEKLY_BUDGET") or "150")
LOCAL_POLLING: bool = os.getenv("LOCAL_POLLING", "false").lower() == "true"
DASHBOARD_USER: str = os.getenv("DASHBOARD_USER") or "admin"
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD") or ""
