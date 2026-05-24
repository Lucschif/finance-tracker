#!/usr/bin/env python
"""
Compute TimesFM 2.5 forecasts for all holdings + total portfolio.

Run locally (NOT on Render — model requires ~1 GB RAM):
    pip install -r requirements-forecast.txt
    python compute_forecasts.py

Results are written to Supabase (DATABASE_URL must be set in .env).
The dashboard reads these cached forecasts; no model runs on the server.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

HORIZON = 90  # days ahead to forecast


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv optional; export vars manually if needed


def _fx_to_eur(currency: str) -> float:
    if currency.upper() == "EUR":
        return 1.0
    try:
        import yfinance as yf
        t = yf.Ticker(f"{currency.upper()}EUR=X")
        rate = t.fast_info.last_price
        return float(rate) if rate else 1.0
    except Exception:
        return 1.0


def fetch_price_series(symbol: str, asset_type: str, years: int = 3) -> np.ndarray | None:
    """Fetch daily closing prices in EUR as a float32 numpy array."""
    import yfinance as yf

    yf_symbol = symbol
    if asset_type == "crypto" and "-" not in symbol.upper():
        yf_symbol = f"{symbol.upper()}-EUR"

    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period=f"{years}y")
        if hist.empty:
            log.warning("  No price history for %s", yf_symbol)
            return None

        currency = getattr(ticker.fast_info, "currency", None) or "USD"
        fx = _fx_to_eur(currency)

        prices = (hist["Close"] * fx).values.astype(np.float32)
        # Remove any NaNs at the tail
        prices = prices[~np.isnan(prices)]
        return prices if len(prices) >= 30 else None
    except Exception as exc:
        log.warning("  Failed to fetch %s: %s", yf_symbol, exc)
        return None


def run_forecast(model, prices: np.ndarray) -> tuple[list[float], list[float], list[float]]:
    """Return (point, q10, q90) as Python lists of length HORIZON."""
    point_arr, quantile_arr = model.forecast(horizon=HORIZON, inputs=[prices])
    # quantile_arr shape: (1, HORIZON, 10)
    # index 0 = mean, 1 = 10th pct, ..., 9 = 90th pct
    point = [max(0.0, float(v)) for v in point_arr[0]]
    q10   = [max(0.0, float(v)) for v in quantile_arr[0, :, 1]]
    q90   = [max(0.0, float(v)) for v in quantile_arr[0, :, 9]]
    return point, q10, q90


def save_forecast(session, symbol: str, dates: list[str],
                  point: list[float], q10: list[float], q90: list[float]):
    from app.database import Forecast

    # Replace any existing forecast for this symbol (keep DB tidy)
    session.query(Forecast).filter(Forecast.symbol == symbol.upper()).delete()

    f = Forecast(
        symbol=symbol.upper(),
        computed_at=datetime.utcnow(),
        forecast_dates=json.dumps(dates),
        point_forecast=json.dumps([round(v, 4) for v in point]),
        q10_forecast=json.dumps([round(v, 4) for v in q10]),
        q90_forecast=json.dumps([round(v, 4) for v in q90]),
    )
    session.add(f)


def main():
    _load_env()

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or db_url.startswith("sqlite"):
        log.warning(
            "DATABASE_URL is not set to a Postgres URL. "
            "Set it in .env to write forecasts to Supabase."
        )

    from app.database import init_db, get_db, get_holdings
    init_db()

    # Forecast date labels for the next HORIZON days
    today = date.today()
    forecast_dates = [
        (today + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        for i in range(HORIZON)
    ]

    # Load TimesFM 2.5
    log.info("Loading TimesFM 2.5 (200M) — this may take 30–60 s on first run...")
    try:
        import torch
        import timesfm
        torch.set_float32_matmul_precision("high")
        model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            "google/timesfm-2.5-200m-pytorch"
        )
        model.compile(
            timesfm.ForecastConfig(
                max_context=1024,
                max_horizon=256,
                normalize_inputs=True,
                use_continuous_quantile_head=True,
                fix_quantile_crossing=True,
                infer_is_positive=True,
            )
        )
    except ImportError:
        log.error("timesfm not installed. Run: pip install -r requirements-forecast.txt")
        sys.exit(1)

    log.info("Model ready.")

    with get_db() as session:
        holdings = get_holdings(session)

    if not holdings:
        log.warning("No holdings in database. Add some via the Telegram bot first.")
        return

    # Per-holding forecasts (values in EUR = price × quantity)
    total_point = [0.0] * HORIZON
    total_q10   = [0.0] * HORIZON
    total_q90   = [0.0] * HORIZON
    computed_count = 0

    for h in holdings:
        log.info("Fetching price history for %s ...", h.symbol)
        prices = fetch_price_series(h.symbol, h.asset_type)
        if prices is None:
            log.warning("  Skipping %s — not enough data.", h.symbol)
            continue

        log.info("  %d data points. Running forecast...", len(prices))
        try:
            point_price, q10_price, q90_price = run_forecast(model, prices)
        except Exception as exc:
            log.warning("  Forecast failed for %s: %s", h.symbol, exc)
            continue

        # Scale by quantity to get EUR value
        qty = h.quantity
        point_eur = [v * qty for v in point_price]
        q10_eur   = [v * qty for v in q10_price]
        q90_eur   = [v * qty for v in q90_price]

        with get_db() as session:
            save_forecast(session, h.symbol, forecast_dates, point_eur, q10_eur, q90_eur)
        log.info("  Saved forecast for %s.", h.symbol)

        # Accumulate for portfolio total
        for i in range(HORIZON):
            total_point[i] += point_eur[i]
            total_q10[i]   += q10_eur[i]
            total_q90[i]   += q90_eur[i]
        computed_count += 1

    # Save portfolio total forecast
    if computed_count > 0:
        with get_db() as session:
            save_forecast(session, "TOTAL", forecast_dates, total_point, total_q10, total_q90)
        log.info("Saved TOTAL portfolio forecast (%d holdings).", computed_count)
    else:
        log.warning("No forecasts computed — check yfinance connectivity.")

    log.info("Done. Reload the dashboard to see the forecast chart.")


if __name__ == "__main__":
    main()
