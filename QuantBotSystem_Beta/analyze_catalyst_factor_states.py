"""Terminal-only diagnostic for point-in-time catalyst tier and decay states."""
from __future__ import annotations

import argparse
import os
from collections import defaultdict

import numpy as np
import pandas as pd

from catalyst_pit_overlay import HOLDING_DAYS, PointInTimeCatalystOverlay
from scalping_catalyst_engine import load_scalping_events
from tune_momentum_no_lookahead import load_price_map, selected_universe


def _age_bucket(age):
    if age <= 3:
        return "fresh_0_3"
    if age <= 10:
        return "medium_4_10"
    return "stale_11_19"


def _state_dates(overlay):
    states = set()
    for record in overlay.records:
        prices = overlay.price_map[record["ticker"]]
        dates = pd.DatetimeIndex(prices.index).date
        start = int(np.searchsorted(dates, record["event_date"], side="left"))
        for value in dates[start : min(len(dates), start + HOLDING_DAYS)]:
            states.add((record["ticker"], value))
    return sorted(states)


def main():
    parser = argparse.ArgumentParser(description="Point-in-time catalyst state diagnostic; terminal output only")
    parser.add_argument("workbook", help="scalping research workbook")
    parser.add_argument("--horizons", default="1,5,10,20", help="forward trading-day horizons")
    args = parser.parse_args()
    if not os.path.exists(args.workbook):
        raise SystemExit("Workbook not found.")
    horizons = [int(value.strip()) for value in args.horizons.split(",") if value.strip()]
    if not horizons or any(value <= 0 for value in horizons):
        raise SystemExit("--horizons must contain positive integers.")

    price_map = load_price_map(selected_universe())
    overlay = PointInTimeCatalystOverlay(load_scalping_events(args.workbook), price_map)
    grouped = defaultdict(list)
    for ticker, signal_date in _state_dates(overlay):
        prices = price_map[ticker]
        dates = pd.DatetimeIndex(prices.index).date
        signal_idx = int(np.searchsorted(dates, signal_date, side="left"))
        entry_idx = signal_idx + 1
        if entry_idx >= len(prices):
            continue
        factor = overlay.factor_on(ticker, signal_date)
        if factor["catalyst_long_tier"] == 0:
            continue
        entry_open = float(prices["Open"].iloc[entry_idx])
        if entry_open <= 0:
            continue
        for horizon in horizons:
            exit_idx = entry_idx + horizon - 1
            if exit_idx >= len(prices):
                continue
            forward_return = float(prices["Close"].iloc[exit_idx]) / entry_open - 1.0
            grouped[(factor["catalyst_long_tier"], _age_bucket(factor["catalyst_age_trading_days"]), horizon)].append(forward_return)

    rows = []
    for (tier, age_bucket, horizon), values in sorted(grouped.items()):
        values = np.asarray(values, dtype=float)
        rows.append({
            "tier": tier,
            "age_bucket": age_bucket,
            "horizon_days": horizon,
            "observations": len(values),
            "mean_return": float(values.mean()),
            "median_return": float(np.median(values)),
            "win_rate": float((values > 0).mean()),
        })
    if not rows:
        raise SystemExit("No valid catalyst states.")
    print("Point-in-time forward-return diagnostic. Overlapping states are not independent observations.")
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda value: f"{value:.4f}"))


if __name__ == "__main__":
    main()
