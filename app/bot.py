from __future__ import annotations

import functools
import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import budget as budget_module
from app import config
from app.database import (
    _as_date,
    add_transaction,
    delete_holding,
    get_accounts,
    get_active_transactions,
    get_db,
    get_holdings,
    set_account_balance,
    undo_last_transaction,
    upsert_holding,
)
from app import prices as prices_module
from app.parser import parse_transaction

logger = logging.getLogger(__name__)


# ── Auth decorator ────────────────────────────────────────────────────────────

def _owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if config.TELEGRAM_OWNER_CHAT_ID and update.effective_chat.id != config.TELEGRAM_OWNER_CHAT_ID:
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, ctx)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 10) -> str:
    filled = int(min(pct / 100, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _budget_line(session) -> str:
    stats = budget_module.weekly_stats(session, config.WEEKLY_BUDGET)
    emoji = "🔴" if stats["pct_used"] >= 100 else "🟡" if stats["pct_used"] >= 75 else "🟢"
    bar = _bar(stats["pct_used"])
    return (
        f"{emoji} Weekly {bar} {stats['pct_used']:.0f}%\n"
        f"   Spent €{stats['weekly_spent']:.2f} / €{stats['weekly_budget']:.2f}"
        f"   · Remaining €{stats['remaining']:.2f}"
    )


# ── Command handlers ──────────────────────────────────────────────────────────

@_owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Finance Tracker*\n\n"
        "Log a transaction by typing naturally:\n"
        "• `14 kebab`\n"
        "• `spent 40 groceries`\n"
        "• `+2400 salary`\n"
        "• `100 to investments`\n"
        "• `40 shoes impulse`\n\n"
        "Commands: /help /today /week /month /income /budget /undo /summary",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n\n"
        "/today — today's transactions\n"
        "/week — this week's summary\n"
        "/month — this month's summary\n"
        "/income — this month's income\n"
        "/budget — weekly budget status\n"
        "/undo — undo last transaction\n"
        "/summary — net worth snapshot\n\n"
        "*Logging*\n"
        "`14 kebab` → expense, Food\n"
        "`+2400 salary` → income\n"
        "`100 to investments` → transfer\n"
        "`40 shoes impulse` → impulse purchase",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    with get_db() as session:
        txns = [t for t in get_active_transactions(session) if _as_date(t.date) == today]
    if not txns:
        await update.message.reply_text("No transactions today yet.")
        return
    lines = [f"*Today — {today}*\n"]
    for t in reversed(txns):
        sign = "+" if t.type == "income" else "-" if t.type == "expense" else "→"
        imp = " ⚡" if t.is_impulse else ""
        lines.append(f"{sign}€{t.amount:.2f}  {t.note or t.category}{imp}")
    spent = sum(t.amount for t in txns if t.type == "expense")
    income = sum(t.amount for t in txns if t.type == "income")
    lines.append(f"\n💸 Spent €{spent:.2f}   💰 Income €{income:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_owner_only
async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    ws = today - timedelta(days=today.weekday())
    with get_db() as session:
        txns = [t for t in get_active_transactions(session) if _as_date(t.date) >= ws]
        budget_str = _budget_line(session)
    spent = sum(t.amount for t in txns if t.type == "expense")
    income = sum(t.amount for t in txns if t.type == "income")
    by_cat: dict[str, float] = {}
    for t in txns:
        if t.type == "expense":
            by_cat[t.category] = by_cat.get(t.category, 0.0) + t.amount
    lines = [f"*This Week ({ws} – {today})*\n", f"💸 Spent €{spent:.2f}", f"💰 Income €{income:.2f}", f"📊 Net €{income - spent:+.2f}"]
    if by_cat:
        lines.append("\n*By Category*")
        for cat, amt in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {cat}: €{amt:.2f}")
    lines.append(f"\n{budget_str}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_owner_only
async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        summary = budget_module.monthly_summary(session)
    lines = [
        f"*{summary['month']}*\n",
        f"💸 Spent €{summary['monthly_spent']:.2f}",
        f"💰 Income €{summary['monthly_income']:.2f}",
        f"📊 Cash Flow €{summary['net_cashflow']:+.2f}",
    ]
    if summary["impulse_total"] > 0:
        lines.append(f"⚡ Impulse €{summary['impulse_total']:.2f}")
    if summary["by_category"]:
        lines.append("\n*By Category*")
        for cat, amt in summary["by_category"].items():
            lines.append(f"  {cat}: €{amt:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_owner_only
async def cmd_income(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        summary = budget_module.monthly_summary(session)
    await update.message.reply_text(
        f"*Income — {summary['month']}*\n\n💰 €{summary['monthly_income']:.2f}",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        line = _budget_line(session)
    await update.message.reply_text(line)


@_owner_only
async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        t = undo_last_transaction(session)
    if t:
        await update.message.reply_text(
            f"↩️ Undone: {t.type} €{t.amount:.2f} — {t.category}\n"
            f"Note: {t.note or '—'}"
        )
    else:
        await update.message.reply_text("No recent transaction to undo.")


@_owner_only
async def cmd_holding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Add / update a holding:
      /holding VWCE.DE 10.5
      /holding BTC 0.05 crypto
      /holding VWCE.DE 0 → removes it
    """
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "*Usage:*\n"
            "`/holding VWCE.DE 10.5`         — ETF (default)\n"
            "`/holding BTC 0.05 crypto`       — crypto\n"
            "`/holding CSPX.L 5 stock`        — stock\n"
            "`/holding VWCE.DE 0`             — remove holding\n\n"
            "Use yfinance tickers: VWCE.DE, IWDA.AS, CSPX.L, BTC, ETH…",
            parse_mode="Markdown",
        )
        return

    symbol = args[0].upper()
    try:
        quantity = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Invalid quantity.")
        return

    asset_type = args[2].lower() if len(args) > 2 else "etf"
    if asset_type not in ("etf", "stock", "crypto"):
        asset_type = "etf"

    if quantity == 0:
        with get_db() as session:
            removed = delete_holding(session, symbol)
        msg = f"🗑 Removed *{symbol}*" if removed else f"❌ *{symbol}* not found."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    with get_db() as session:
        upsert_holding(session, symbol, quantity, asset_type)

    price = prices_module.get_price_eur(symbol, asset_type)
    if price:
        value = price * quantity
        await update.message.reply_text(
            f"✅ *{symbol}* — {quantity:g} units\n"
            f"   Price: €{price:,.2f}   Value: €{value:,.2f}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ *{symbol}* saved ({quantity:g} units)\n"
            f"⚠️ Could not fetch price — check the ticker symbol.",
            parse_mode="Markdown",
        )


@_owner_only
async def cmd_holdings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        holdings = get_holdings(session)
    if not holdings:
        await update.message.reply_text(
            "No holdings yet. Add one with:\n`/holding VWCE.DE 10.5`",
            parse_mode="Markdown",
        )
        return

    portfolio = prices_module.get_portfolio_value(holdings)
    lines = ["*📈 Portfolio*\n"]
    for item in portfolio["holdings"]:
        price_str = f"€{item['price']:,.2f}" if item["price"] else "n/a"
        value_str = f"€{item['value']:,.2f}" if item["value"] else "n/a"
        lines.append(f"*{item['symbol']}* ({item['quantity']:g} units)")
        lines.append(f"   {price_str} × {item['quantity']:g} = {value_str}")
    lines.append(f"\n💼 *Total: €{portfolio['total']:,.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_owner_only
async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /setup cash 5000 investments 15000"""
    args = ctx.args
    if not args or len(args) < 2 or len(args) % 2 != 0:
        await update.message.reply_text(
            "*Usage:* `/setup cash 5000 investments 15000`\n\n"
            "Sets the t=0 baseline for each account. "
            "Run once (or any time you want to re-anchor your starting point).",
            parse_mode="Markdown",
        )
        return

    results = []
    with get_db() as session:
        i = 0
        while i < len(args) - 1:
            name = args[i].lower()
            try:
                balance = float(args[i + 1].replace(",", "."))
            except ValueError:
                await update.message.reply_text(f"❌ Invalid amount for `{args[i]}`", parse_mode="Markdown")
                return
            set_account_balance(session, name, balance)
            results.append(f"  *{name.capitalize()}*: €{balance:,.2f}")
            i += 2

    lines = ["✅ *Baseline set*\n"] + results + ["\nThis is your t=0 net worth anchor."]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@_owner_only
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with get_db() as session:
        accounts = get_accounts(session)
        initial = sum(a.initial_balance or 0 for a in accounts)
        all_txns = get_active_transactions(session)
        total_income = sum(t.amount for t in all_txns if t.type == "income")
        total_expense = sum(t.amount for t in all_txns if t.type == "expense")
        net_worth = initial + total_income - total_expense
        budget_str = _budget_line(session)
        summary = budget_module.monthly_summary(session)
    lines = [
        "*💼 Summary*\n",
        f"🏦 Net Worth: €{net_worth:.2f}",
        f"📊 {summary['month']}: €{summary['net_cashflow']:+.2f} net",
        f"",
        budget_str,
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Message handler ───────────────────────────────────────────────────────────

@_owner_only
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parsed = await parse_transaction(text)
    if not parsed:
        await update.message.reply_text(
            "❓ Couldn't parse that. Try:\n`14 kebab`  `+2400 salary`  `100 to investments`",
            parse_mode="Markdown",
        )
        return

    with get_db() as session:
        add_transaction(
            session,
            amount=parsed["amount"],
            type_=parsed["type"],
            category=parsed["category"],
            note=parsed.get("note", ""),
            is_impulse=parsed.get("is_impulse", False),
        )
        budget_str = _budget_line(session) if parsed["type"] == "expense" else ""

    emoji = {"income": "💰", "expense": "💸", "transfer": "🔄"}.get(parsed["type"], "💸")
    sign = "+" if parsed["type"] == "income" else "-" if parsed["type"] == "expense" else "→"
    imp_tag = "  ⚡ *impulse*" if parsed.get("is_impulse") else ""

    lines = [
        f"{emoji} {sign}€{parsed['amount']:.2f} — *{parsed['category']}*{imp_tag}",
        f"📝 _{parsed.get('note') or '—'}_",
    ]
    if budget_str:
        lines += ["", budget_str]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Factory ───────────────────────────────────────────────────────────────────

def create_ptb_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("holding", cmd_holding))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("income", cmd_income))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
