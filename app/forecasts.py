"""Read pre-computed TimesFM forecasts from the database."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def get_latest_forecast(symbol: str) -> dict | None:
    """Return the most recent forecast for *symbol*, or None if none stored."""
    from app.database import get_db, Forecast
    try:
        with get_db() as session:
            f = (
                session.query(Forecast)
                .filter(Forecast.symbol == symbol.upper())
                .order_by(Forecast.computed_at.desc())
                .first()
            )
            if f is None:
                return None
            return {
                "computed_at": f.computed_at.isoformat() if f.computed_at else None,
                "dates": json.loads(f.forecast_dates),
                "point": json.loads(f.point_forecast),
                "q10": json.loads(f.q10_forecast),
                "q90": json.loads(f.q90_forecast),
            }
    except Exception as exc:
        logger.warning("get_latest_forecast failed for %s: %s", symbol, exc)
        return None


def get_total_forecast_noncrypto(holdings) -> dict | None:
    """Sum individual non-crypto holding forecasts to produce a crypto-free TOTAL.

    Falls back to None if no per-holding forecasts are stored yet.
    """
    from app.database import get_db, Forecast

    non_crypto_symbols = [h.symbol for h in holdings if h.asset_type != "crypto"]
    if not non_crypto_symbols:
        return None
    try:
        with get_db() as session:
            rows = (
                session.query(Forecast)
                .filter(Forecast.symbol.in_(non_crypto_symbols))
                .order_by(Forecast.computed_at.desc())
                .all()
            )
            # Keep only the latest forecast per symbol
            latest: dict = {}
            for f in rows:
                if f.symbol not in latest:
                    latest[f.symbol] = f

            if not latest:
                return None

            all_dates = None
            total_point: list[float] = []
            total_q10:   list[float] = []
            total_q90:   list[float] = []
            oldest_at = None

            for f in latest.values():
                dates  = json.loads(f.forecast_dates)
                point  = json.loads(f.point_forecast)
                q10    = json.loads(f.q10_forecast)
                q90    = json.loads(f.q90_forecast)
                if all_dates is None:
                    all_dates    = dates
                    total_point  = [0.0] * len(point)
                    total_q10    = [0.0] * len(q10)
                    total_q90    = [0.0] * len(q90)
                n = min(len(point), len(total_point))
                for i in range(n):
                    total_point[i] += point[i]
                    total_q10[i]   += q10[i]
                    total_q90[i]   += q90[i]
                if f.computed_at:
                    oldest_at = f.computed_at if oldest_at is None else min(oldest_at, f.computed_at)

            if all_dates is None:
                return None
            return {
                "computed_at": oldest_at.isoformat() if oldest_at else None,
                "dates":  all_dates,
                "point":  total_point,
                "q10":    total_q10,
                "q90":    total_q90,
            }
    except Exception as exc:
        logger.warning("get_total_forecast_noncrypto failed: %s", exc)
        return None


def get_forecast_symbols() -> list[str]:
    """Return all symbols that have at least one stored forecast, TOTAL first."""
    from app.database import get_db, Forecast
    from sqlalchemy import distinct
    try:
        with get_db() as session:
            rows = session.query(distinct(Forecast.symbol)).all()
            syms = [r[0] for r in rows]
            # Put TOTAL first, then alphabetical
            total = [s for s in syms if s == "TOTAL"]
            rest = sorted(s for s in syms if s != "TOTAL")
            return total + rest
    except Exception as exc:
        logger.warning("get_forecast_symbols failed: %s", exc)
        return []
