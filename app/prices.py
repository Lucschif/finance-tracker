"""Live price fetching for ETFs, stocks and crypto.

ETFs/stocks: yfinance (e.g. VWCE.DE, CSPX.L, IWDA.AS)
Crypto:      yfinance EUR pairs (e.g. BTC-EUR, ETH-EUR, SOL-EUR)

Prices are cached for 5 minutes so the dashboard doesn't hammer
the APIs on every page load.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, float]] = {}       # symbol -> (price, timestamp)
_hist_cache: dict[str, tuple[list, list, float]] = {}  # key -> (labels, values, timestamp)
_TTL = 300  # seconds

# Proxy map for extending history of newer share classes via an older equivalent.
# Used only for the MAX range (projection stats) — not regular chart fetches.
# VWRL.L is the distributing share class of the same Vanguard FTSE All-World fund,
# listed on LSE since 2012 vs VWCE.DE's 2019. Total returns are virtually identical.
_PROXY_MAP: dict[str, str] = {
    "VWCE.DE": "VWRL.L",
}


# ── Public API ────────────────────────────────────────────────────────────────

def get_price_eur(symbol: str, asset_type: str) -> float | None:
    """Return current EUR price for symbol, using cache."""
    key = symbol.upper()
    if key in _cache:
        price, ts = _cache[key]
        if time.time() - ts < _TTL:
            return price
    price = _fetch(symbol, asset_type)
    if price is not None:
        _cache[key] = (price, time.time())
    return price


def get_portfolio_value(holdings) -> dict:
    """Return live EUR value for a list of Holding ORM objects."""
    total = 0.0
    items = []
    for h in holdings:
        price = get_price_eur(h.symbol, h.asset_type)
        value = round(price * h.quantity, 2) if price is not None else None
        if value == 0.0 and price is not None:
            value = price * h.quantity  # keep precision for tiny-price assets
        items.append({
            "symbol": h.symbol,
            "name": h.name or h.symbol,
            "quantity": h.quantity,
            "price": price,
            "value": value,
            "asset_type": h.asset_type,
        })
        if value is not None:
            total += value
    items.sort(key=lambda x: x["value"] if x["value"] is not None else -1, reverse=True)
    return {"holdings": items, "total": round(total, 2)}


def get_portfolio_history(holdings, range_str: str) -> tuple[list[str], list[float]]:
    """Return (labels, values) of total portfolio EUR value over time."""
    if not holdings:
        return [], []
    cache_key = range_str + "|" + ",".join(f"{h.symbol}:{h.quantity}" for h in holdings)
    if cache_key in _hist_cache:
        labels, values, ts = _hist_cache[cache_key]
        if time.time() - ts < _TTL:
            return labels, values
    result = _fetch_portfolio_history(holdings, range_str)
    _hist_cache[cache_key] = (result[0], result[1], time.time())
    return result


def maybe_take_snapshot() -> bool:
    """Take today's portfolio snapshot if one doesn't exist yet.

    Captures the live EUR value of every holding and stores it in
    ``portfolio_snapshots``.  Safe to call from both the dashboard (FastAPI)
    and the Telegram bot — always fire-and-forget via a thread so it never
    blocks a response.

    Returns True if a new snapshot was written, False if one already existed
    or if no holdings are configured.
    """
    try:
        import json
        from app import database as db_module

        today = date.today()
        with db_module.get_db() as session:
            if db_module.get_portfolio_snapshot(session, today):
                return False  # already taken today
            holdings = db_module.get_holdings(session)

        if not holdings:
            return False

        portfolio = get_portfolio_value(holdings)
        if portfolio["total"] == 0.0:
            return False

        breakdown = {
            item["symbol"]: round(item["value"], 6)
            for item in portfolio["holdings"]
            if item["value"] is not None
        }

        with db_module.get_db() as session:
            db_module.save_portfolio_snapshot(
                session,
                today,
                round(portfolio["total"], 2),
                json.dumps(breakdown),
            )
        logger.info("Portfolio snapshot saved: €%.2f", portfolio["total"])
        return True
    except Exception as exc:
        logger.warning("maybe_take_snapshot failed: %s", exc)
        return False


def get_portfolio_history_from_snapshots(range_str: str) -> tuple[list[str], list[float]]:
    """Return (labels, values) of portfolio history read from DB snapshots.

    Falls back to empty lists if no snapshots exist yet — callers should
    check ``len(values) < 2`` and fall back to the yfinance path if needed.
    """
    try:
        from app import database as db_module

        today = date.today()
        range_days: dict[str, int] = {
            "1D": 1, "7D": 7, "14D": 14, "30D": 30, "90D": 90,
        }
        if range_str in range_days:
            since = today - timedelta(days=range_days[range_str])
        elif range_str == "YTD":
            since = date(today.year, 1, 1)
        else:  # ALL
            since = None

        with db_module.get_db() as session:
            snapshots = db_module.get_portfolio_snapshots(session, since=since)

        if not snapshots:
            return [], []

        labels = [db_module._as_date(s.date).strftime("%b %d") for s in snapshots]
        values = [round(s.total_eur, 2) for s in snapshots]
        return labels, values
    except Exception as exc:
        logger.warning("get_portfolio_history_from_snapshots failed: %s", exc)
        return [], []


def get_portfolio_projection(holdings, symbol: str, years: int,
                              history_years: int | None = None) -> dict | None:
    """CAGR + volatility-cone projection for total portfolio or a single non-crypto holding.

    Crypto is always excluded — too volatile for CAGR to be meaningful.
    *history_years* controls how many years of past data are used to estimate
    the annualised return and volatility (None = use all available data).
    The chart context on the left also reflects this window.
    """
    # Crypto is excluded from all projections
    non_crypto = [h for h in holdings if h.asset_type != "crypto"]
    if symbol.upper() == "TOTAL":
        target = non_crypto
    else:
        target = [h for h in non_crypto if h.symbol == symbol.upper()]
    if not target:
        return None

    # Full history for the selected holdings
    labels_all, values_all = get_portfolio_history(target, "MAX")
    if len(values_all) < 60:
        return None

    try:
        import numpy as np

        # Slice to the requested history window for stat computation
        if history_years is not None:
            n = min(history_years * 252, len(values_all))
        else:
            n = len(values_all)
        n = max(n, 30)  # need at least 30 points

        values_window = values_all[-n:]
        labels_window = labels_all[-n:]

        v = np.array(values_window, dtype=np.float64)
        v = v[v > 0]
        if len(v) < 30:
            return None

        log_ret      = np.diff(np.log(v))
        mu_annual    = float(log_ret.mean() * 252)
        sigma_annual = float(log_ret.std() * np.sqrt(252))
        v0           = float(v[-1])

        today = date.today()
        proj_labels, proj_point, proj_upper, proj_lower = [], [], [], []
        for m in range(1, years * 12 + 1):
            future = today + timedelta(days=m * 30)
            t = m / 12.0
            pt = v0 * np.exp(mu_annual * t)
            up = v0 * np.exp(mu_annual * t + sigma_annual * np.sqrt(t))
            lo = max(0.0, v0 * np.exp(mu_annual * t - sigma_annual * np.sqrt(t)))
            proj_labels.append(future.strftime("%b %Y"))
            proj_point.append(round(float(pt), 2))
            proj_upper.append(round(float(up), 2))
            proj_lower.append(round(float(lo), 2))

        # Chart context = the history window used for stats
        ctx = min(252, len(labels_window))  # cap at 1 year for readability
        return {
            "hist_labels": labels_window[-ctx:],
            "hist_values": [round(float(x), 2) for x in values_window[-ctx:]],
            "proj_labels": proj_labels,
            "proj_point":  proj_point,
            "proj_upper":  proj_upper,
            "proj_lower":  proj_lower,
            "cagr":       round((float(np.exp(mu_annual)) - 1) * 100, 1),
            "vol":        round(sigma_annual * 100, 1),
            "hist_years": round(len(values_window) / 252, 1),
        }
    except Exception as exc:
        logger.warning("get_portfolio_projection failed for %s: %s", symbol, exc)
        return None


def _fetch_portfolio_history(holdings, range_str: str) -> tuple[list[str], list[float]]:
    try:
        import yfinance as yf
        import pandas as pd

        today = date.today()
        range_days = {
            "1D": 2, "7D": 7, "14D": 14, "30D": 30,
            "90D": 90, "YTD": (today - date(today.year, 1, 1)).days + 1,
            "ALL": 365,
        }
        if range_str in range_days:
            start = today - timedelta(days=range_days[range_str])
        else:  # MAX — used internally by projection to get full history
            start = today - timedelta(days=365 * 35)

        end = today + timedelta(days=1)  # yfinance end is exclusive

        all_series = []
        for h in holdings:
            symbol = h.symbol
            if h.asset_type == "crypto" and "-" not in symbol.upper():
                symbol = f"{symbol.upper()}-EUR"

            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(start=start.isoformat(), end=end.isoformat())
                if hist.empty:
                    continue
                currency = getattr(ticker.fast_info, "currency", None) or "USD"
                fx = 1.0
                if currency.upper() != "EUR":
                    fx = _fx_to_eur(currency.upper()) or 1.0
                series = (hist["Close"] * h.quantity * fx).rename(h.symbol)

                # For MAX range, prepend proxy history where available
                if range_str == "MAX" and h.symbol in _PROXY_MAP:
                    proxy_sym = _PROXY_MAP[h.symbol]
                    try:
                        p_ticker = yf.Ticker(proxy_sym)
                        p_hist = p_ticker.history(start=start.isoformat(), end=end.isoformat())
                        if not p_hist.empty:
                            p_currency = (getattr(p_ticker.fast_info, "currency", None) or "USD").upper()
                            if p_currency == "EUR":
                                p_eur = p_hist["Close"]
                            else:
                                # Fetch historical FX rates so each day uses the correct rate,
                                # not today's spot (avoids baking GBP/EUR moves into equity returns)
                                fx_hist = yf.Ticker(f"{p_currency}EUR=X").history(
                                    start=start.isoformat(), end=end.isoformat()
                                )
                                if fx_hist.empty:
                                    raise ValueError(f"no FX history for {p_currency}EUR=X")
                                fx_series = fx_hist["Close"].reindex(p_hist.index).ffill().bfill()
                                p_eur = p_hist["Close"] * fx_series
                            actual_start = series.index[0]
                            p_before = p_eur[p_eur.index < actual_start]
                            if len(p_before) >= 20:
                                # Scale proxy so its last value matches the actual's first value
                                scale = series.iloc[0] / p_before.iloc[-1]
                                series = pd.concat([(p_before * scale).rename(h.symbol), series])
                    except Exception as exc:
                        logger.warning("proxy fetch failed for %s via %s: %s", h.symbol, proxy_sym, exc)

                all_series.append(series)
            except Exception as exc:
                logger.warning("history fetch failed for %s: %s", h.symbol, exc)

        if not all_series:
            return [], []

        combined = pd.concat(all_series, axis=1)
        combined = combined.ffill().bfill()  # bfill fills missing early data from first known price
        total = combined.sum(axis=1)

        labels = [d.strftime("%b %d") for d in total.index]
        values = [round(float(v), 2) for v in total.values]
        return labels, values
    except Exception as exc:
        logger.warning("portfolio history failed: %s", exc)
        return [], []


# ── Fetching ──────────────────────────────────────────────────────────────────

def _fetch(symbol: str, asset_type: str) -> float | None:
    # For crypto, ensure the symbol ends in -EUR so yfinance returns EUR price
    if asset_type == "crypto" and "-" not in symbol.upper():
        symbol = f"{symbol.upper()}-EUR"
    return _yfinance(symbol)


def _yfinance(ticker: str) -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # Try fast_info first, fall back to history for low-cap assets
        price = t.fast_info.last_price
        if price is None:
            hist = t.history(period="1d")
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])

        currency = getattr(t.fast_info, "currency", None) or "USD"
        if currency.upper() != "EUR":
            rate = _fx_to_eur(currency.upper())
            if rate is None:
                logger.warning("No FX rate for %s → EUR", currency)
                return None
            price = price * rate
        return float(price)  # keep full precision for tiny prices
    except Exception as exc:
        logger.warning("yfinance failed for %s: %s", ticker, exc)
        return None


def _fx_to_eur(from_currency: str) -> float | None:
    if from_currency == "EUR":
        return 1.0
    try:
        import yfinance as yf
        t = yf.Ticker(f"{from_currency}EUR=X")
        rate = t.fast_info.last_price
        return float(rate) if rate else None
    except Exception:
        return None
