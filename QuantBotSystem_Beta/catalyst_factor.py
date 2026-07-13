# File name: catalyst_factor.py
# Provides fixed catalyst overlay scores for existing strategy engines.
import os

import config
import market_data_store
import scalping_catalyst_engine as catalyst
from catalyst_pit_overlay import PointInTimeCatalystOverlay


def _resolve_workbook_path():
    path = getattr(config, "SCALPING_WORKBOOK_PATH", "")
    if not path:
        path = "../스캘핑 ver 2.0 - 복사본.xlsx"
    base_dir = os.path.dirname(__file__)
    candidates = [path] if os.path.isabs(path) else [
        os.path.abspath(os.path.join(base_dir, path)),
        os.path.abspath(os.path.join(base_dir, "../스캘핑 ver 2.0 - 복사본.xlsx")),
        os.path.abspath(os.path.join(base_dir, "../../스캘핑 ver 2.0 - 복사본.xlsx")),
        os.path.abspath(os.path.join(base_dir, "../scalping_sandbox.xlsx")),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def latest_catalyst_map(universe):
    workbook_path = _resolve_workbook_path()
    if not os.path.exists(workbook_path):
        return {}

    universe_set = set(universe)
    records = catalyst.load_scalping_events(workbook_path)
    latest = {}
    for record in records:
        ticker = record.get("ticker")
        event_date = record.get("event_date")
        if ticker not in universe_set or not event_date:
            continue
        if ticker not in latest or event_date > latest[ticker].get("event_date", ""):
            score = catalyst.fixed_catalyst_score(record)
            enriched = dict(record)
            enriched["fixed_score"] = score
            enriched["confirmed"] = score >= catalyst.FIXED_THRESHOLD
            latest[ticker] = enriched
    return latest


def latest_point_in_time_map(universe):
    """Return current, non-leaking catalyst diagnostics for the Beta engines.

    This intentionally leaves ``latest_catalyst_map`` intact for legacy Beta
    engines. Momentum calls this function instead of the fixed-score API.
    """
    workbook_path = _resolve_workbook_path()
    if not os.path.exists(workbook_path):
        return {}
    price_map = {}
    for ticker in sorted(set(universe)):
        prices = market_data_store.get_price_data(ticker)
        if not prices.empty:
            price_map[ticker] = prices
    if not price_map:
        return {}
    overlay = PointInTimeCatalystOverlay(catalyst.load_scalping_events(workbook_path), price_map)
    result = {}
    for ticker, prices in price_map.items():
        as_of_date = prices.index[-1].date()
        result[ticker] = overlay.factor_on(ticker, as_of_date)
    return result


def confirmed_score(catalyst_map, ticker):
    row = catalyst_map.get(ticker)
    if not row or not row.get("confirmed"):
        return 0.0
    return float(row.get("fixed_score", 0.0))


def describe(catalyst_map, ticker):
    row = catalyst_map.get(ticker)
    if not row:
        return "Catalyst: none"
    score = float(row.get("fixed_score", 0.0))
    label = "confirmed" if row.get("confirmed") else "weak"
    drivers = []
    for text, key in [
        ("1D > 7%", "is_7pct_up"),
        ("52W high", "is_52w_high"),
        ("estimate up", "is_estimate_up"),
        ("turnover top10", "is_turnover_top10"),
        ("52W low", "is_52w_low"),
    ]:
        if row.get(key):
            drivers.append(text)
    driver_text = ", ".join(drivers) if drivers else "analyst score"
    return f"Catalyst {label} {score:.1f} ({driver_text})"
