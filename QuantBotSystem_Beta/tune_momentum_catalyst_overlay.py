"""Validation-first tuning for the point-in-time catalyst overlay on MA momentum."""
from __future__ import annotations

import argparse
import os

import pandas as pd

from catalyst_pit_overlay import PointInTimeCatalystOverlay
from run_catalyst_overlay_ab import _candidate_rows, _load_sector_map, _segments, simulate
from scalping_catalyst_engine import load_scalping_events
from tune_momentum_no_lookahead import load_price_map, selected_universe


def _parse_csv(value, cast):
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def _parse_windows_grid(value):
    grids = []
    for item in value.split(";"):
        windows = tuple(_parse_csv(item, int))
        if len(windows) != 3 or tuple(sorted(windows)) != windows:
            raise argparse.ArgumentTypeError("each --windows-grid value must be fast,mid,slow")
        grids.append(windows)
    return grids


def _row(segment, parameters, model, result, baseline):
    overall = result["overall"]
    baseline_overall = baseline["overall"]
    return {
        "segment": segment,
        **parameters,
        "model": model,
        "total_return": overall["total_return"],
        "sharpe": overall["sharpe"],
        "mdd": overall["mdd"],
        "volatility": overall["volatility"],
        "turnover": overall["turnover"],
        "trades": overall["trades"],
        "incremental_return": overall["total_return"] - baseline_overall["total_return"],
        "incremental_sharpe": overall["sharpe"] - baseline_overall["sharpe"],
        "incremental_mdd": overall["mdd"] - baseline_overall["mdd"],
    }


def main():
    parser = argparse.ArgumentParser(description="Validation-first tuner: MA momentum + point-in-time catalyst overlay")
    parser.add_argument("workbook", help="scalping research workbook")
    parser.add_argument("--windows-grid", default="5,20,60;5,10,20;10,20,60")
    parser.add_argument("--slope-grid", default="0,0.2,0.4")
    parser.add_argument("--holding-grid", default="20", help="Keep 20 to match the catalyst research horizon")
    parser.add_argument("--top-n-grid", default="3,5,10")
    parser.add_argument("--tilts", default="0.05,0.10,0.15")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--short-penalty-tier1", type=float, default=0.03)
    parser.add_argument("--short-penalty-tier2", type=float, default=0.08)
    parser.add_argument("--min-validation-trades", type=int, default=20)
    parser.add_argument("--sector-map")
    parser.add_argument("--output", help="Optional CSV path; terminal output is the default")
    parser.add_argument("--include-nonpositive", action="store_true", help="Also show validation candidates without positive total and incremental return")
    args = parser.parse_args()
    if not os.path.exists(args.workbook):
        raise SystemExit("Workbook not found.")

    price_map = load_price_map(selected_universe())
    overlay = PointInTimeCatalystOverlay(load_scalping_events(args.workbook), price_map)
    sector_map = _load_sector_map(args.sector_map)
    penalties = {0: 0.0, 1: args.short_penalty_tier1, 2: args.short_penalty_tier2}
    rows = []
    for windows in _parse_windows_grid(args.windows_grid):
        for slope in _parse_csv(args.slope_grid, float):
            for holding_days in _parse_csv(args.holding_grid, int):
                candidates = _candidate_rows(price_map, overlay, holding_days, windows, slope, sector_map)
                if not candidates:
                    continue
                for top_n in _parse_csv(args.top_n_grid, int):
                    parameters = {"windows": "/".join(map(str, windows)), "slope_threshold": slope, "holding_days": holding_days, "top_n": top_n, "cost_bps": args.cost_bps}
                    models = {"baseline": (0.0, {0: 0.0, 1: 0.0, 2: 0.0})}
                    for tilt in _parse_csv(args.tilts, float):
                        models[f"long_overlay_{tilt:.2f}"] = (tilt, {0: 0.0, 1: 0.0, 2: 0.0})
                        models[f"combined_{tilt:.2f}"] = (tilt, penalties)
                    for segment, selector in _segments(candidates).items():
                        scoped = {date_value: values for date_value, values in candidates.items() if selector(date_value)}
                        results = {name: simulate(scoped, tilt, penalty, top_n, args.cost_bps) for name, (tilt, penalty) in models.items()}
                        baseline = results["baseline"]
                        rows.extend(_row(segment, parameters, name, result, baseline) for name, result in results.items())

    if not rows:
        raise SystemExit("No valid tuning results.")
    output = pd.DataFrame(rows)
    validation = output[(output.segment == "validation") & (output.model != "baseline") & (output.trades >= args.min_validation_trades)].copy()
    if not args.include_nonpositive:
        validation = validation[(validation.total_return > 0) & (validation.incremental_return > 0)]
    if args.output:
        output.to_csv(args.output, index=False, encoding="utf-8-sig")
    if validation.empty:
        print(f"No positive validation overlay candidate met --min-validation-trades={args.min_validation_trades}.")
        return
    validation = validation.sort_values(["incremental_sharpe", "incremental_return", "incremental_mdd"], ascending=[False, False, False])
    selected = validation.head(10)
    print("Positive validation overlay candidates (test was not used for selection):")
    print(selected[["model", "windows", "slope_threshold", "holding_days", "top_n", "trades", "total_return", "sharpe", "mdd", "incremental_return", "incremental_sharpe"]].to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    if args.output:
        print(f"\nOptional local CSV written: {args.output}")


if __name__ == "__main__":
    main()
