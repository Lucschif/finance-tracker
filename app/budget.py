from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.database import get_active_transactions, _as_date


def _week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def _month_start() -> date:
    return date.today().replace(day=1)


def weekly_stats(db: Session, budget: float) -> dict:
    ws = _week_start()
    txns = [
        t for t in get_active_transactions(db)
        if t.type == "expense" and _as_date(t.date) >= ws
    ]
    spent = sum(t.amount for t in txns)
    remaining = max(0.0, budget - spent)
    pct = (spent / budget * 100) if budget else 0.0
    return {
        "weekly_spent": spent,
        "weekly_budget": budget,
        "remaining": remaining,
        "pct_used": pct,
    }


def monthly_summary(db: Session) -> dict:
    ms = _month_start()
    all_active = get_active_transactions(db)
    month_txns = [t for t in all_active if _as_date(t.date) >= ms]

    income = sum(t.amount for t in month_txns if t.type == "income")
    expenses = [t for t in month_txns if t.type == "expense"]
    spent = sum(t.amount for t in expenses)
    impulse_total = sum(t.amount for t in expenses if t.is_impulse)

    by_cat: dict[str, float] = {}
    for t in expenses:
        by_cat[t.category] = by_cat.get(t.category, 0.0) + t.amount
    by_cat = dict(sorted(by_cat.items(), key=lambda x: x[1], reverse=True))

    return {
        "month": date.today().strftime("%B %Y"),
        "monthly_spent": spent,
        "monthly_income": income,
        "net_cashflow": income - spent,
        "impulse_total": impulse_total,
        "by_category": by_cat,
    }
