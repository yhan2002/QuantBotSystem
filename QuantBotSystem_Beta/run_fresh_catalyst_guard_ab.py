"""A/B test fresh tier-2 catalyst controls on the MA momentum strategy."""
from __future__ import annotations

import argparse
import os

import pandas as pd

from catalyst_pit_overlay import PointInTimeCatalystOverlay
from run_catalyst_overlay_ab import _candidate_rows, _load_sector_map, _parse_windows, _segments, simulate
from scalping_catalyst_engine import load_scalping_events
from tune_momentum_no_lookahead import load_price_map, selected_universe


def _summary_row(segment, model, result, baseline):
    overall = result["overall"]
    baseline_overall = baseline["overall"]
    return {
        "segment": segment,
        "model": model,
        "return": overall["total_return"],
        "sharpe": overall["sharpe"],
        "mdd": overall["mdd"],
        "trades": overall["trades"],
        "incremental_return": overall["total_return"] - baseline_overall["total_return"],
        "incremental_sharpe": overall["sharpe"] - baseline_overall["sharpe"],
    }


def main():
    parser = argparse.ArgumentParser(description="Fresh tier-2 catalyst guard A/B: MA momentum")
    parser.add_argument("workbook", help="scalping research workbook")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--fresh-days", type=int, default=3)
    parser.add_argument("--tiebreak-max-rank-gap", type=int, default=1)
    parser.add_argument("--windows", type=_parse_windows, default=(5, 20, 60))
    parser.add_argument("--slope-threshold", type=float, default=0.0)
    parser.add_argument("--holding-days", type=int, default=20)
    parser.add_argument("--train-end")
    parser.add_argument("--validation-end")
    parser.add_argument("--sector-map")
    args = parser.parse_args()
    if not os.path.exists(args.workbook):
        raise SystemExit("Workbook not found.")
    if min(args.top_n, args.fresh_days, args.holding_days) <= 0 or args.tiebreak_max_rank_gap < 0:
        raise SystemExit("top-n, fresh-days, and holding-days must be positive; rank gap cannot be negative.")

    price_map = load_price_map(selected_universe())
    overlay = PointInTimeCatalystOverlay(load_scalping_events(args.workbook), price_map)
    candidates = _candidate_rows(price_map, overlay, args.holding_days, args.windows, args.slope_threshold, _load_sector_map(args.sector_map))
    if not candidates:
        raise SystemExit("No valid candidates. Verify cached price history and workbook ticker mapping.")

    models = {
        "A_baseline": {},
        "B_long_tiebreaker": {"fresh_long_tiebreaker": True},
        "C_short_exclusion": {"fresh_short_exclusion": True},
        "D_combined": {"fresh_long_tiebreaker": True, "fresh_short_exclusion": True},
    }
    rows = []
    for segment, selector in _segments(candidates, args.train_end, args.validation_end).items():
        scoped = {date_value: values for date_value, values in candidates.items() if selector(date_value)}
        results = {
            name: simulate(
                scoped,
                long_tilt=0.0,
                short_penalties={0: 0.0, 1: 0.0, 2: 0.0},
                max_positions=args.top_n,
                cost_bps=args.cost_bps,
                fresh_days=args.fresh_days,
                tiebreak_max_rank_gap=args.tiebreak_max_rank_gap,
                **controls,
            )
            for name, controls in models.items()
        }
        baseline = results["A_baseline"]
        rows.extend(_summary_row(segment, name, result, baseline) for name, result in results.items())

    print("Fresh tier-2 condition: catalyst_long_tier == 2 and age <= %d trading days" % args.fresh_days)
    print("Test is reported only after validation; do not select parameters from test.")
    print(pd.DataFrame(rows).to_string(index=False, float_format=lambda value: f"{value:.4f}"))


if __name__ == "__main__":
    main()
