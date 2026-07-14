# Compares the Beta MA momentum baseline with a turnover-surge alpha overlay.
import argparse
import math

import pandas as pd

import config
import market_data_store
import tune_momentum_no_lookahead as momentum_bt


DEFAULT_WINDOWS = (5, 20, 60)
DEFAULT_SLOPE_THRESHOLD = 0.0
DEFAULT_HOLDING_DAYS = 20
DEFAULT_LOOKBACK = 252
DEFAULT_TURNOVER_MULTIPLE = 5.0
DEFAULT_ALPHA_WEIGHT = 0.5
DEFAULT_TOP_N = 5
DEFAULT_MODE = "LONG_BIASED_70_30"


def selected_universe(groups):
    tickers = set()
    for group in groups:
        tickers.update(config.universe_groups.get(group, []))
    return sorted(tickers)


def add_volume_alpha_columns(df, lookback, turnover_multiple):
    """Build the delay-1-safe inputs using close as the unavailable VWAP proxy."""
    out = df.copy()
    daily_return = out["Close"].pct_change()
    positive = (daily_return > 0).astype(float).where(daily_return.notna())
    out["POS_DAYS"] = positive.rolling(lookback, min_periods=lookback).sum()
    out["TURNOVER_PROXY"] = out["Volume"].astype(float) * out["Close"].astype(float)
    out["TURNOVER_MEAN"] = out["TURNOVER_PROXY"].rolling(
        lookback,
        min_periods=lookback,
    ).mean()
    out["TURNOVER_SURGE"] = (
        (out["TURNOVER_MEAN"] > 0)
        & (out["TURNOVER_PROXY"] > out["TURNOVER_MEAN"] * turnover_multiple)
    )
    out["REVERSION_5D"] = -out["Close"].pct_change(5)
    return out


def build_combined_signal_map(price_map, windows, threshold, lookback, turnover_multiple):
    signal_map = {}
    alpha_panels = []
    required_rows = max(windows) + 2

    for ticker, df in price_map.items():
        if len(df) < required_rows:
            continue
        frame = momentum_bt.build_signal_frame(df, windows, threshold)
        frame["Volume"] = df["Volume"].astype(float)
        frame = add_volume_alpha_columns(frame, lookback, turnover_multiple)
        signal_map[ticker] = frame

        panel = frame[["POS_DAYS", "REVERSION_5D"]].copy()
        panel["ticker"] = ticker
        alpha_panels.append(panel.reset_index().rename(columns={panel.index.name or "index": "date"}))

    if not alpha_panels:
        return signal_map

    panel = pd.concat(alpha_panels, ignore_index=True)
    panel = panel.dropna(subset=["POS_DAYS", "REVERSION_5D"])
    if panel.empty:
        return signal_map

    date_group = panel.groupby("date")
    x_centered = panel["REVERSION_5D"] - date_group["REVERSION_5D"].transform("mean")
    y_centered = panel["POS_DAYS"] - date_group["POS_DAYS"].transform("mean")
    numerator = (x_centered * y_centered).groupby(panel["date"]).transform("sum")
    denominator = (x_centered * x_centered).groupby(panel["date"]).transform("sum")
    beta = numerator.div(denominator.where(denominator > 0)).fillna(0.0)
    panel["ALPHA_NEUT"] = y_centered - beta * x_centered

    for ticker, rows in panel.groupby("ticker"):
        if ticker not in signal_map:
            continue
        values = rows.set_index("date")["ALPHA_NEUT"]
        signal_map[ticker].loc[values.index, "ALPHA_NEUT"] = values

    return signal_map


def build_combined_events(signal_map, windows, holding_days, lookback, alpha_weight):
    events_by_date = {}
    start_idx = max(max(windows), lookback)

    for ticker, signals in signal_map.items():
        if len(signals) < start_idx + holding_days + 1:
            continue

        for idx in range(start_idx, len(signals) - holding_days):
            if not bool(signals["TURNOVER_SURGE"].iloc[idx]):
                continue
            alpha_value = signals["ALPHA_NEUT"].iloc[idx]
            if pd.isna(alpha_value):
                continue

            entry_idx = idx + 1
            exit_idx = idx + holding_days
            entry_price = float(signals["Open"].iloc[entry_idx])
            exit_price = float(signals["Close"].iloc[exit_idx])
            if entry_price <= 0:
                continue

            signal_date = signals.index[idx]
            daily = events_by_date.setdefault(signal_date, {"LONG": [], "SHORT": []})
            if bool(signals["LONG_SIGNAL"].iloc[idx]):
                daily["LONG"].append(
                    {
                        "ticker": ticker,
                        "side": "LONG",
                        "momentum_score": float(signals["LONG_SCORE"].iloc[idx]),
                        "alpha_score": float(alpha_value),
                        "entry_idx": entry_idx,
                        "exit_idx": exit_idx,
                        "return": (exit_price / entry_price - 1.0) * 100.0,
                    }
                )
            if bool(signals["SHORT_SIGNAL"].iloc[idx]):
                daily["SHORT"].append(
                    {
                        "ticker": ticker,
                        "side": "SHORT",
                        "momentum_score": float(signals["SHORT_SCORE"].iloc[idx]),
                        "alpha_score": -float(alpha_value),
                        "entry_idx": entry_idx,
                        "exit_idx": exit_idx,
                        "return": (entry_price - exit_price) / entry_price * 100.0,
                    }
                )

    prepared = {}
    for date, sides in events_by_date.items():
        prepared[date] = {"LONG": [], "SHORT": []}
        for side in ("LONG", "SHORT"):
            rows = sides[side]
            if not rows:
                continue
            momentum_values = pd.Series([row["momentum_score"] for row in rows])
            alpha_values = pd.Series([row["alpha_score"] for row in rows])
            momentum_ranks = momentum_values.rank(method="average", pct=True)
            alpha_ranks = alpha_values.rank(method="average", pct=True)
            for row, momentum_rank, alpha_rank in zip(rows, momentum_ranks, alpha_ranks):
                row["score"] = (
                    (1.0 - alpha_weight) * float(momentum_rank)
                    + alpha_weight * float(alpha_rank)
                )
            prepared[date][side] = sorted(rows, key=lambda row: row["score"], reverse=True)
    return prepared


def format_summary(label, summary):
    if not summary:
        return f"{label:<16} | no valid closed trades"
    return (
        f"{label:<16} | cohorts={summary['cohorts']:>4} | trades={summary['trades']:>5} | "
        f"avg={summary['avg']:+.3f}% | t={summary['t']:+.2f} | "
        f"cohort win={summary['win']:.1f}% | trade win={summary['trade_win']:.1f}%"
    )


def parse_windows(value):
    parts = tuple(int(part) for part in value.split(","))
    if len(parts) != 3 or any(part <= 0 for part in parts):
        raise argparse.ArgumentTypeError("windows must be three positive integers, e.g. 5,20,60")
    return parts


def main():
    parser = argparse.ArgumentParser(
        description="A/B test Beta MA momentum versus the turnover-surge alpha overlay."
    )
    parser.add_argument("--groups", nargs="+", default=momentum_bt.DEFAULT_GROUPS)
    parser.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS)
    parser.add_argument("--slope", type=float, default=DEFAULT_SLOPE_THRESHOLD)
    parser.add_argument("--holding-days", type=int, default=DEFAULT_HOLDING_DAYS)
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument("--turnover-multiple", type=float, default=DEFAULT_TURNOVER_MULTIPLE)
    parser.add_argument("--alpha-weight", type=float, default=DEFAULT_ALPHA_WEIGHT)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--backfill-period",
        choices=["2y", "5y", "10y"],
        help="explicitly download and merge longer history before the backtest",
    )
    parser.add_argument(
        "--mode",
        choices=["LONG_ONLY", "LONG_BIASED_70_30", "LONG_SHORT_50_50"],
        default=DEFAULT_MODE,
    )
    args = parser.parse_args()

    if not 0.0 <= args.alpha_weight <= 1.0:
        parser.error("--alpha-weight must be between 0 and 1")
    if args.lookback < 2 or args.holding_days < 1 or args.turnover_multiple <= 0:
        parser.error("lookback, holding-days, and turnover-multiple must be positive")

    universe = selected_universe(args.groups)
    if args.backfill_period:
        print(
            f"Backfilling {len(universe)} tickers with period={args.backfill_period}. "
            "This can take several minutes.",
            flush=True,
        )

        def progress(idx, total, ticker):
            if idx == 1 or idx == total or idx % 25 == 0:
                print(f"Backfill {idx}/{total}: {ticker}", flush=True)

        summary = market_data_store.backfill_price_data(
            universe,
            period=args.backfill_period,
            progress_callback=progress,
        )
        print(
            f"Backfill complete: rows merged={summary['updated']} | "
            f"failed={len(summary['failed'])}",
            flush=True,
        )

    price_map = momentum_bt.load_price_map(universe)
    row_counts = [len(df) for df in price_map.values()]
    required_rows = args.lookback + args.holding_days + 1
    eligible = sum(count >= required_rows for count in row_counts)

    print("Momentum + Volume Alpha A/B Backtest")
    print("=" * 78)
    print("Signal at close; execution at next open; no look-ahead.")
    print("VWAP proxy: Close. Turnover proxy: Volume * Close.")
    print(
        "Combined: MA signal AND turnover surge; ranking = "
        f"{1.0 - args.alpha_weight:.0%} momentum + {args.alpha_weight:.0%} "
        "reversion-neutralized positive-day alpha."
    )
    print(
        f"Groups={','.join(args.groups)} | tickers={len(universe)} | local={len(price_map)} | "
        f"rows min/max={min(row_counts) if row_counts else 0}/{max(row_counts) if row_counts else 0}"
    )
    print(
        f"MA={args.windows} | slope={args.slope:.2f}% | hold={args.holding_days}d | "
        f"lookback={args.lookback}d | turnover>{args.turnover_multiple:.1f}x | "
        f"top={args.top_n} | mode={args.mode}"
    )
    print(f"Exact-alpha eligible tickers: {eligible}/{len(price_map)} (required rows: {required_rows})")
    print("-" * 78)

    baseline_map, _ = momentum_bt.build_signal_map(price_map, args.windows, args.slope)
    baseline_events = momentum_bt.build_events(baseline_map, args.windows, args.holding_days)
    baseline_summary = momentum_bt.simulate(baseline_events, args.top_n, args.mode)

    combined_map = build_combined_signal_map(
        price_map,
        args.windows,
        args.slope,
        args.lookback,
        args.turnover_multiple,
    )
    combined_events = build_combined_events(
        combined_map,
        args.windows,
        args.holding_days,
        args.lookback,
        args.alpha_weight,
    )
    combined_summary = momentum_bt.simulate(combined_events, args.top_n, args.mode)

    print(format_summary("Baseline MA", baseline_summary))
    print(format_summary("MA + Alpha 1", combined_summary))
    if baseline_summary and combined_summary:
        print("-" * 78)
        print(
            f"Delta avg={combined_summary['avg'] - baseline_summary['avg']:+.3f}% | "
            f"delta t={combined_summary['t'] - baseline_summary['t']:+.2f} | "
            f"trade coverage={combined_summary['trades'] / baseline_summary['trades']:.1%}"
        )
    elif eligible == 0:
        print(
            "Combined result unavailable: the local DB does not contain enough history. "
            "Use at least lookback + holding-days + 1 daily rows per ticker."
        )


if __name__ == "__main__":
    main()
