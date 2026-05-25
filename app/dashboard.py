from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from telegram import Update

from app import budget as budget_module
from app import categories as categories_module
from app import config
from app import database as db
from app import forecasts as forecasts_module
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

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Middleware-level limiter so it fires BEFORE FastAPI's auth dependency.
# Default: 30 req/min per IP. Public routes (/health /ping /webhook) are exempt.
limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


def _fmt_hours(h) -> str:
    try:
        h = float(h or 0)
    except (TypeError, ValueError):
        h = 0.0
    total_min = round(h * 60)
    hrs, mins = divmod(total_min, 60)
    if hrs and mins:
        return f"{hrs}h {mins}min"
    if hrs:
        return f"{hrs}h"
    return f"{mins}min"


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
templates.env.filters["fmt_hours"] = _fmt_hours

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
@limiter.exempt
async def health():
    return {"status": "ok"}


@app.get("/ping")
@limiter.exempt
async def ping():
    return {"pong": True}


@app.post("/webhook")
@limiter.exempt
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

    # Live portfolio value + history (fetched outside the DB session — may call yfinance)
    portfolio = prices_module.get_portfolio_value(holdings) if holdings else {"holdings": [], "total": 0.0}
    inv_chart_labels, inv_chart_values = prices_module.get_portfolio_history(holdings, range_str) if holdings else ([], [])

    # Net worth = cash baseline + transaction delta + live investments
    cash_baseline = sum(a.initial_balance or 0 for a in accounts if a.name.lower() == "cash")
    inv_baseline  = sum(a.initial_balance or 0 for a in accounts if a.name.lower() != "cash")
    investments_value = portfolio["total"] if holdings else inv_baseline
    live_cash = cash_baseline + total_income - total_expense  # cash moves with every income/expense
    live_nw = live_cash + investments_value
    full_baseline = cash_baseline + investments_value

    # Rebuild chart with full baseline (cash + portfolio)
    chart_labels, chart_values = _build_chart_data(all_txns, full_baseline, range_str)

    # Forecasts (pre-computed offline, read from DB — may be None if never computed)
    total_forecast = forecasts_module.get_latest_forecast("TOTAL")
    forecast_symbols = forecasts_module.get_forecast_symbols()

    return templates.TemplateResponse(request, "financials.html", {
        "active_page": "financials",
        "live_nw": live_nw,
        "cash_baseline": live_cash,
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
        "inv_chart_labels": inv_chart_labels,
        "inv_chart_values": inv_chart_values,
        "total_forecast": total_forecast,
        "forecast_symbols": forecast_symbols,
        "activity_feed": activity_feed,
    })


@app.get("/investments")
async def investments_page(request: Request, _: None = Depends(_auth)):
    range_str = request.query_params.get("range", "ALL").upper()
    if range_str not in ("1D", "7D", "14D", "30D", "90D", "YTD", "ALL"):
        range_str = "ALL"

    with db.get_db() as session:
        holdings = db.get_holdings(session)

    portfolio = prices_module.get_portfolio_value(holdings) if holdings else {"holdings": [], "total": 0.0}
    inv_chart_labels, inv_chart_values = prices_module.get_portfolio_history(holdings, range_str) if holdings else ([], [])
    total_forecast = forecasts_module.get_latest_forecast("TOTAL")
    forecast_symbols = forecasts_module.get_forecast_symbols()

    return templates.TemplateResponse(request, "investments.html", {
        "active_page": "investments",
        "portfolio": portfolio,
        "inv_chart_labels": inv_chart_labels,
        "inv_chart_values": inv_chart_values,
        "total_forecast": total_forecast,
        "forecast_symbols": forecast_symbols,
        "chart_ranges": ["1D", "7D", "14D", "30D", "90D", "YTD", "ALL"],
        "selected_range": range_str,
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


@app.get("/api/forecast")
async def forecast_api(request: Request, _: None = Depends(_auth)):
    symbol = request.query_params.get("symbol", "TOTAL").upper()
    range_str = request.query_params.get("range", "ALL").upper()
    if range_str not in ("1D", "7D", "14D", "30D", "90D", "YTD", "ALL"):
        range_str = "ALL"

    with db.get_db() as session:
        holdings = db.get_holdings(session)

    non_crypto = [h for h in holdings if h.asset_type != "crypto"]

    if symbol == "TOTAL":
        hist_labels, hist_values = prices_module.get_portfolio_history(non_crypto, range_str) if non_crypto else ([], [])
        forecast = forecasts_module.get_total_forecast_noncrypto(holdings)
    else:
        holding = next((h for h in non_crypto if h.symbol == symbol), None)
        if holding:
            hist_labels, hist_values = prices_module.get_portfolio_history([holding], range_str)
            forecast = forecasts_module.get_latest_forecast(symbol)
        else:
            hist_labels, hist_values = [], []
            forecast = None

    available = forecasts_module.get_forecast_symbols()

    return {
        "symbol": symbol,
        "range": range_str,
        "hist_labels": hist_labels,
        "hist_values": hist_values,
        "forecast": forecast,
        "available_symbols": available,
    }


@app.get("/api/projection")
async def projection_api(request: Request, _: None = Depends(_auth)):
    symbol = request.query_params.get("symbol", "TOTAL").upper()
    try:
        years = int(request.query_params.get("years", "5"))
    except ValueError:
        years = 5
    years = max(1, min(years, 10))

    raw_hy = request.query_params.get("history_years", "")
    history_years: int | None = None
    if raw_hy.isdigit():
        history_years = max(1, min(int(raw_hy), 20))

    with db.get_db() as session:
        holdings = db.get_holdings(session)

    result = prices_module.get_portfolio_projection(holdings, symbol, years, history_years)
    if result is None:
        return {"error": "Not enough history to compute projection (crypto is excluded)", "symbol": symbol, "years": years}
    return {"symbol": symbol, "years": years, "history_years": history_years, **result}


@app.get("/productivity")
async def productivity_page(request: Request, _: None = Depends(_auth)):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = date(today.year, today.month, 1)
    thirty_days_ago = today - timedelta(days=29)

    with db.get_db() as session:
        all_sessions = db.get_productivity_sessions(session)
        recent = db.get_recent_productivity_sessions(session, limit=20)

    today_hours = round(sum(s.duration_hours for s in all_sessions if db._as_date(s.date) == today), 2)
    week_hours = round(sum(s.duration_hours for s in all_sessions if db._as_date(s.date) >= week_start), 2)
    month_hours = round(sum(s.duration_hours for s in all_sessions if db._as_date(s.date) >= month_start), 2)

    # Streak: consecutive days ending today (or yesterday as grace) with ≥1 session
    session_dates = {db._as_date(s.date) for s in all_sessions}
    streak_start = today if today in session_dates else today - timedelta(days=1)
    streak = 0
    if streak_start in session_dates:
        cur = streak_start
        while cur in session_dates:
            streak += 1
            cur -= timedelta(days=1)

    # Stacked bar: hours by category for each day of this week
    week_labels, week_work, week_study, week_personal = [], [], [], []
    for i in range(7):
        d = week_start + timedelta(days=i)
        day = [s for s in all_sessions if db._as_date(s.date) == d]
        week_labels.append(d.strftime("%a"))
        week_work.append(round(sum(s.duration_hours for s in day if s.category == "Work"), 2))
        week_study.append(round(sum(s.duration_hours for s in day if s.category == "Study"), 2))
        week_personal.append(round(sum(s.duration_hours for s in day if s.category == "Personal Project"), 2))

    # 30-day line: total hours per day
    daily: dict[date, float] = defaultdict(float)
    for s in all_sessions:
        d = db._as_date(s.date)
        if d >= thirty_days_ago:
            daily[d] += s.duration_hours
    line_labels = [(thirty_days_ago + timedelta(days=i)).strftime("%b %d") for i in range(30)]
    line_values = [round(daily.get(thirty_days_ago + timedelta(days=i), 0.0), 2) for i in range(30)]

    return templates.TemplateResponse(request, "productivity.html", {
        "active_page": "productivity",
        "month_label": today.strftime("%B %Y"),
        "today_hours": today_hours,
        "week_hours": week_hours,
        "month_hours": month_hours,
        "streak": streak,
        "week_labels": week_labels,
        "week_work": week_work,
        "week_study": week_study,
        "week_personal": week_personal,
        "line_labels": line_labels,
        "line_values": line_values,
        "recent_sessions": recent,
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
