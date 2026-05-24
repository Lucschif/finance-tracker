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
