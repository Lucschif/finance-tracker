from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telegram import Update

from app import budget as budget_module
from app import categories as categories_module
from app import config
from app import database as db
from app import prices as prices_module

_ptb_app = None


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ptb_app
    db.init_db()
    if config.TELEGRAM_BOT_TOKEN and not config.LOCAL_POLLING:
        from app.bot import create_ptb_app
        _ptb_app = create_ptb_app()
        await _ptb_app.initialize()
        await _ptb_app.start()
        if config.WEBHOOK_BASE_URL:
            await _ptb_app.bot.set_webhook(
                url=f"{config.WEBHOOK_BASE_URL.rstrip('/')}/webhook",
                secret_token=config.TELEGRAM_WEBHOOK_SECRET or None,
            )
    yield
    if _ptb_app:
        await _ptb_app.stop()
        await _ptb_app.shutdown()


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _fmt_currency(value) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"€{v:,.2f}"


def _budget_color(pct: float) -> str:
    if pct >= 100:
        return "danger"
    if pct >= 75:
        return "warning"
    return "accent"


templates.env.filters["format_currency"] = _fmt_currency
templates.env.filters["budget_color"] = _budget_color

_security = HTTPBasic()

def _auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not config.DASHBOARD_PASSWORD:
        return  # no password set → open (local dev)
    ok_user = secrets.compare_digest(credentials.username, config.DASHBOARD_USER)
    ok_pass = secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── Chart helpers ─────────────────────────────────────────────────────────────

def _build_chart_data(txns, initial_balance: float, range_str: str):
    today = date.today()
    if range_str == "1D":
        start_date = today - timedelta(days=1)
    elif range_str == "7D":
        start_date = today - timedelta(days=7)
    elif range_str == "14D":
        start_date = today - timedelta(days=14)
    elif range_str == "30D":
        start_date = today - timedelta(days=30)
    elif range_str == "90D":
        start_date = today - timedelta(days=90)
    elif range_str == "YTD":
        start_date = date(today.year, 1, 1)
    else:
        if txns:
            try:
                start_date = min(db._as_date(t.date) for t in txns)
            except Exception:
                start_date = today
        else:
            start_date = today

    daily_delta: dict[date, float] = defaultdict(float)
    for t in txns:
        d = db._as_date(t.date)
        if t.type == "income":
            daily_delta[d] += t.amount
        elif t.type == "expense":
            daily_delta[d] -= t.amount

    base_nw = initial_balance
    for t in txns:
        d = db._as_date(t.date)
        if d < start_date:
            if t.type == "income":
                base_nw += t.amount
            elif t.type == "expense":
                base_nw -= t.amount

    labels: list[str] = []
    values: list[float] = []
    current_nw = base_nw
    current_date = start_date
    while current_date <= today:
        current_nw += daily_delta.get(current_date, 0.0)
        labels.append(current_date.strftime("%b %d"))
        values.append(round(current_nw, 2))
        current_date += timedelta(days=1)

    if len(labels) > 90:
        step = max(len(labels) // 90, 1)
        labels = labels[::step]
        values = values[::step]

    return labels, values


def _build_activity_feed(txns) -> list[dict]:
    def sort_key(t):
        d = db._as_date(t.date)
        ts = t.created_at if t.created_at else datetime(d.year, d.month, d.day)
        return ts

    recent = sorted(txns, key=sort_key, reverse=True)[:20]
    feed = []
    for t in recent:
        impact = t.amount if t.type == "income" else -t.amount if t.type == "expense" else 0.0
        ts = t.created_at.isoformat() if t.created_at else str(t.date)
        feed.append({
            "event_type": t.type,
            "timestamp": ts,
            "description": t.note or t.category,
            "amount": t.amount,
            "impact": impact,
            "is_impulse": t.is_impulse,
        })
    return feed


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}




@app.get("/ping")
async def ping():
    return {"pong": True}


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    if not _ptb_app:
        return Response(status_code=200)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if config.TELEGRAM_WEBHOOK_SECRET and secret != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    data = await request.json()
    update = Update.de_json(data, _ptb_app.bot)
    await _ptb_app.process_update(update)
    return Response(status_code=200)


@app.get("/")
async def financials_page(request: Request, _: None = Depends(_auth)):
    range_str = request.query_params.get("range", "ALL").upper()
    if range_str not in ("1D", "7D", "14D", "30D", "90D", "YTD", "ALL"):
        range_str = "ALL"

    with db.get_db() as session:
        accounts = db.get_accounts(session)
        holdings = db.get_holdings(session)
        initial_balance = sum(a.initial_balance or 0 for a in accounts)
        all_txns = db.get_active_transactions(session)
        total_income = sum(t.amount for t in all_txns if t.type == "income")
        total_expense = sum(t.amount for t in all_txns if t.type == "expense")
        monthly = budget_module.monthly_summary(session)
        chart_labels, chart_values = _build_chart_data(all_txns, initial_balance, range_str)
        activity_feed = _build_activity_feed(all_txns)

    # Live portfolio value (fetched outside the DB session — may call yfinance)
    portfolio = prices_module.get_portfolio_value(holdings) if holdings else {"holdings": [], "total": 0.0}

    # Net worth = cash baseline + transaction delta + live investments
    cash_baseline = sum(a.initial_balance or 0 for a in accounts if a.name.lower() == "cash")
    inv_baseline  = sum(a.initial_balance or 0 for a in accounts if a.name.lower() != "cash")
    investments_value = portfolio["total"] if holdings else inv_baseline
    live_nw = cash_baseline + total_income - total_expense + investments_value
    full_baseline = cash_baseline + investments_value

    # Rebuild chart with full baseline (cash + portfolio)
    chart_labels, chart_values = _build_chart_data(all_txns, full_baseline, range_str)

    return templates.TemplateResponse(request, "financials.html", {
        "active_page": "financials",
        "live_nw": live_nw,
        "cash_baseline": cash_baseline,
        "investments_value": investments_value,
        "initial_balance": full_baseline,
        "accounts": [a for a in accounts if (a.initial_balance or 0) > 0],
        "portfolio": portfolio,
        "monthly_change": monthly["net_cashflow"],
        "monthly": monthly,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "chart_ranges": ["1D", "7D", "14D", "30D", "90D", "YTD", "ALL"],
        "selected_range": range_str,
        "activity_feed": activity_feed,
    })


@app.get("/expenses")
async def expenses_page(request: Request, _: None = Depends(_auth)):
    with db.get_db() as session:
        weekly = budget_module.weekly_stats(session, config.WEEKLY_BUDGET)
        monthly = budget_module.monthly_summary(session)
        recent = db.get_recent_transactions(session, limit=30)

    return templates.TemplateResponse(request, "expenses.html", {
        "active_page": "expenses",
        "weekly": weekly,
        "monthly": monthly,
        "recent_transactions": recent,
    })


@app.get("/categories")
async def categories_page(request: Request, _: None = Depends(_auth)):
    with db.get_db() as session:
        monthly = budget_module.monthly_summary(session)

    top_category = (
        max(monthly["by_category"], key=monthly["by_category"].get)
        if monthly["by_category"] else None
    )
    max_amount = max(monthly["by_category"].values()) if monthly["by_category"] else 1.0

    return templates.TemplateResponse(request, "categories.html", {
        "active_page": "categories",
        "monthly": monthly,
        "top_category": top_category,
        "max_amount": max_amount,
    })


@app.get("/transactions")
async def transactions_page(request: Request, _: None = Depends(_auth)):
    filter_type = request.query_params.get("type", "")
    filter_category = request.query_params.get("category", "")

    with db.get_db() as session:
        all_txns = list(reversed(db.get_all_transactions(session)))

    if filter_type:
        all_txns = [t for t in all_txns if t.type == filter_type]
    if filter_category:
        all_txns = [t for t in all_txns if t.category == filter_category]

    return templates.TemplateResponse(request, "transactions.html", {
        "active_page": "transactions",
        "transactions": all_txns,
        "filter_type": filter_type,
        "filter_category": filter_category,
        "categories": categories_module.ALL_CATEGORIES,
    })
