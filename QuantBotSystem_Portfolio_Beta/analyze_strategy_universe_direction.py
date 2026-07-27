"""Developer-only strategy x universe x direction performance cube.

The analysis uses only the local SQLite cache, close-confirmed signals,
next-open entry, zero trading costs, score-weighted quantities, and explicit
gross/side/name limits. LONG, SHORT, and LS are simulated independently because
an opposite-side position can suppress later entries on the same ticker.
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from statistics import median

import pandas as pd

import config
import core_backtest_periods
import core_entry_execution
import core_market_data_store
import core_portfolio_performance
import runtime_settings
import strategy_box_range
import strategy_ma_cross
import strategy_price_liquidity_reversion


BASE_GROUPS = {
    "CSQ": "CSQ_UNIV",
    "JP": "JP_UNIV",
    "CH": "CH_UNIV",
    "US Top 100": "US_LARGE_CAP_100_20260630_PROXY",
}
UNIVERSE_ORDER = ("CSQ", "JP", "CH", "US Top 100", "Primary Combined")
MODE_SIDES = {"LONG": "LONG", "SHORT": "SHORT", "LS": None}
MODE_POSITION = {
    "LONG": "LONG_ONLY",
    "SHORT": "SHORT_ONLY",
    "LS": "LONG_SHORT_NEUTRAL",
}
DIRECTIONAL_STRATEGIES = ("Momentum", "Box Range", "Reversion")
STRATEGIES = DIRECTIONAL_STRATEGIES
WINDOW_ORDER = ("IS", "OOS")
SUMMARY_COLUMNS = [
    "strategy", "universe", "mode", "window", "evaluation_start", "evaluation_end",
    "universe_count", "eligible_tickers", "failed_ticker_count", "data_coverage_pct",
    "traded_tickers", "traded_ticker_coverage_pct", "trade_count", "long_trade_count",
    "short_trade_count", "wins", "avg_trade_return_pct", "median_trade_return_pct",
    "win_rate_pct", "strategy_roi_pct", "strategy_cagr_pct", "strategy_mdd_pct",
    "strategy_sharpe", "benchmark_roi_pct", "benchmark_cagr_pct", "benchmark_mdd_pct",
    "benchmark_sharpe", "excess_roi_pct", "excess_cagr_pct", "active_days",
    "avg_active_positions", "max_active_positions", "end_active_positions",
    "benchmark_tickers", "benchmark_min_daily_tickers", "benchmark_max_daily_tickers",
    "benchmark_coverage_pct", "avg_gross_exposure_pct", "max_gross_exposure_pct",
    "avg_net_exposure_pct", "end_cash", "box_flat_trade_count",
    "box_flat_win_rate_pct", "box_flat_nav_contribution_pct",
    "box_trend_trade_count", "box_trend_win_rate_pct",
    "box_trend_nav_contribution_pct", "roi_target_applicable", "required_roi_pct",
    "passes_2x_benchmark_roi", "passes_58pct_win_rate", "passes_min_30_trades",
    "status", "error",
]
DAILY_COLUMNS = [
    "strategy", "universe", "mode", "window", "date", "strategy_return_pct",
    "strategy_nav", "strategy_cumulative_roi_pct", "benchmark_return_pct",
    "benchmark_nav", "benchmark_cumulative_roi_pct", "active_positions",
    "cash", "long_market_value", "short_market_value", "gross_exposure_pct",
    "net_exposure_pct", "benchmark_constituents",
]


def _period_bounds(label):
    if not label or label == "N/A" or " to " not in label:
        return None, None
    return tuple(label.split(" to ", 1))


def _trade_exit_date(trade):
    if trade.get("exit_date"):
        return trade["exit_date"]
    events = trade.get("exit_events") or []
    return events[-1].get("date") if events else None


def trade_stats(trades):
    values = [float(trade["return"]) for trade in trades]
    if not values:
        return {"count": 0, "avg": 0.0, "median": 0.0, "win_rate": 0.0, "wins": 0}
    wins = sum(value > 0 for value in values)
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "median": median(values),
        "win_rate": wins / len(values) * 100.0,
        "wins": wins,
    }


def aggregate_cached_results(universe, cached_results):
    """Aggregate already-computed per-ticker runner tuples for one universe."""
    all_trades = []
    eligible_names = []
    failed_tickers = []
    starts, ends = [], []
    for ticker in universe:
        result = cached_results.get(ticker)
        if isinstance(result, Exception) or not result:
            failed_tickers.append(ticker)
            continue
        _, _, _, period_label, details = result
        start, end = _period_bounds(period_label)
        if start and end:
            eligible_names.append(ticker)
            starts.append(start)
            ends.append(end)
        else:
            failed_tickers.append(ticker)
        for trade in details:
            item = dict(trade)
            item["ticker"] = ticker
            item["exit_date"] = _trade_exit_date(item)
            all_trades.append(item)
    all_trades.sort(key=lambda trade: (trade.get("exit_date") or "", trade["ticker"]))
    long_trades = [trade for trade in all_trades if trade.get("side") == "LONG"]
    short_trades = [trade for trade in all_trades if trade.get("side") == "SHORT"]
    return {
        "universe_count": len(universe),
        "eligible_tickers": len(eligible_names),
        "traded_tickers": len({trade["ticker"] for trade in all_trades}),
        "failed_tickers": failed_tickers,
        "period": f"{min(starts)} to {max(ends)}" if starts and ends else "N/A",
        "trades": all_trades,
        "all": trade_stats(all_trades),
        "long": trade_stats(long_trades),
        "short": trade_stats(short_trades),
    }


def target_status(trade_count, win_rate_pct, strategy_roi_pct, benchmark_roi_pct, minimum_trades=30):
    sample_pass = trade_count >= minimum_trades
    target = core_portfolio_performance.evaluate_return_target(
        strategy_roi_pct,
        benchmark_roi_pct,
        win_rate_pct,
        benchmark_multiple=2.0,
        win_rate_goal_pct=58.0,
    )
    if not sample_pass:
        status = "INSUFFICIENT_TRADES"
    else:
        status = target["status"]
    return {
        "roi_target_applicable": target["return_target_applicable"],
        "required_roi_pct": target["required_return_pct"],
        "passes_2x_benchmark_roi": target["passes_return_target"],
        "passes_58pct_win_rate": target["meets_win_rate_goal"],
        "passes_min_30_trades": sample_pass,
        "status": status,
    }


def _universe_specs():
    specs = {
        label: sorted(set(config.universe_groups[group]))
        for label, group in BASE_GROUPS.items()
    }
    specs["Primary Combined"] = sorted(
        set(specs["CSQ"]) | set(specs["JP"]) | set(specs["CH"])
    )
    return specs


def _runner(strategy, ticker, prices, side, window):
    common = {
        "include_trades": True,
        "df": prices,
        "entry_execution": core_entry_execution.NEXT_OPEN,
        **window,
    }
    if strategy == "Momentum":
        return strategy_ma_cross.run_isolated_backtest(
            ticker,
            side_filter=side,
            period=core_backtest_periods.HISTORY_PERIOD,
            **common,
        )
    if strategy == "Box Range":
        return strategy_box_range.run_box_backtest(
            ticker,
            side_filter=side,
            period=core_backtest_periods.HISTORY_PERIOD,
            **common,
        )
    return strategy_price_liquidity_reversion.run_reversion_backtest(
        ticker,
        side=side,
        period_days=1060,
        **common,
    )


def _blank_summary(strategy, universe_name, mode, window_name, result, error):
    start, end = _period_bounds(result.get("period"))
    row = {column: "" for column in SUMMARY_COLUMNS}
    row.update(
        strategy=strategy,
        universe=universe_name,
        mode=mode,
        window=window_name,
        evaluation_start=start or "",
        evaluation_end=end or "",
        universe_count=result.get("universe_count", 0),
        eligible_tickers=result.get("eligible_tickers", 0),
        failed_ticker_count=len(result.get("failed_tickers", [])),
        traded_tickers=result.get("traded_tickers", 0),
        trade_count=result.get("all", {}).get("count", 0),
        long_trade_count=result.get("long", {}).get("count", 0),
        short_trade_count=result.get("short", {}).get("count", 0),
        wins=result.get("all", {}).get("wins", 0),
        avg_trade_return_pct=result.get("all", {}).get("avg", 0.0),
        median_trade_return_pct=result.get("all", {}).get("median", 0.0),
        win_rate_pct=result.get("all", {}).get("win_rate", 0.0),
        status="ERROR" if error else "NO_ELIGIBLE_DATA",
        error=error,
    )
    count = row["universe_count"]
    row["data_coverage_pct"] = row["eligible_tickers"] / count * 100.0 if count else 0.0
    row["traded_ticker_coverage_pct"] = row["traded_tickers"] / count * 100.0 if count else 0.0
    return row


def _performance_row(
    strategy,
    universe_name,
    mode,
    window_name,
    result,
    universe,
    price_data,
    daily_rows,
    max_stale_days,
    minimum_trades,
):
    start, end = _period_bounds(result["period"])
    if not start or not end:
        return _blank_summary(strategy, universe_name, mode, window_name, result, "")
    try:
        performance = core_portfolio_performance.calculate_score_weighted_performance(
            result["trades"],
            universe,
            start,
            end,
            price_loader=lambda ticker: price_data[ticker],
            position_mode=MODE_POSITION[mode],
            max_stale_calendar_days=max_stale_days,
        )
    except Exception as exc:
        return _blank_summary(strategy, universe_name, mode, window_name, result, str(exc))

    daily = performance["daily"]
    for date, values in daily.iterrows():
        daily_rows.append(
            {
                "strategy": strategy,
                "universe": universe_name,
                "mode": mode,
                "window": window_name,
                "date": date.date().isoformat(),
                "strategy_return_pct": float(values["strategy_return"]) * 100.0,
                "strategy_nav": float(values["strategy_nav"]),
                "strategy_cumulative_roi_pct": (float(values["strategy_nav"]) - 1.0) * 100.0,
                "benchmark_return_pct": float(values["benchmark_return"]) * 100.0,
                "benchmark_nav": float(values["benchmark_nav"]),
                "benchmark_cumulative_roi_pct": (float(values["benchmark_nav"]) - 1.0) * 100.0,
                "active_positions": int(values["active_positions"]),
                "cash": float(values["cash"]),
                "long_market_value": float(values["long_market_value"]),
                "short_market_value": float(values["short_market_value"]),
                "gross_exposure_pct": float(values["gross_exposure_pct"]),
                "net_exposure_pct": float(values["net_exposure_pct"]),
                "benchmark_constituents": int(values["benchmark_constituents"]),
            }
        )

    executed_trades = [
        trade
        for trade in performance["ledger"]["trades"]
        if trade.get("allocation_status") == "FILLED"
    ]
    stats = trade_stats(executed_trades)
    long_stats = trade_stats(
        [trade for trade in executed_trades if trade.get("side") == "LONG"]
    )
    short_stats = trade_stats(
        [trade for trade in executed_trades if trade.get("side") == "SHORT"]
    )
    flat_trades = [
        trade
        for trade in executed_trades
        if str(trade.get("structure") or "").upper() == "FLAT"
    ]
    trend_trades = [
        trade
        for trade in executed_trades
        if str(trade.get("structure") or "").upper()
        in ("RISING", "FALLING")
    ]
    flat_stats = trade_stats(flat_trades)
    trend_stats = trade_stats(trend_trades)
    traded_tickers = len({trade["ticker"] for trade in executed_trades})
    strategy_summary = performance["strategy"]
    benchmark_summary = performance["benchmark"]
    universe_count = result["universe_count"]
    target = target_status(
        stats["count"],
        stats["win_rate"],
        strategy_summary["roi_pct"],
        benchmark_summary["roi_pct"],
        minimum_trades=minimum_trades,
    )
    benchmark_tickers = performance["benchmark_tickers"]
    return {
        "strategy": strategy,
        "universe": universe_name,
        "mode": mode,
        "window": window_name,
        "evaluation_start": start,
        "evaluation_end": end,
        "universe_count": universe_count,
        "eligible_tickers": result["eligible_tickers"],
        "failed_ticker_count": len(result["failed_tickers"]),
        "data_coverage_pct": result["eligible_tickers"] / universe_count * 100.0 if universe_count else 0.0,
        "traded_tickers": traded_tickers,
        "traded_ticker_coverage_pct": traded_tickers / universe_count * 100.0 if universe_count else 0.0,
        "trade_count": stats["count"],
        "long_trade_count": long_stats["count"],
        "short_trade_count": short_stats["count"],
        "wins": stats["wins"],
        "avg_trade_return_pct": stats["avg"],
        "median_trade_return_pct": stats["median"],
        "win_rate_pct": stats["win_rate"],
        "strategy_roi_pct": strategy_summary["roi_pct"],
        "strategy_cagr_pct": strategy_summary["cagr_pct"],
        "strategy_mdd_pct": strategy_summary["mdd_pct"],
        "strategy_sharpe": strategy_summary["sharpe"],
        "benchmark_roi_pct": benchmark_summary["roi_pct"],
        "benchmark_cagr_pct": benchmark_summary["cagr_pct"],
        "benchmark_mdd_pct": benchmark_summary["mdd_pct"],
        "benchmark_sharpe": benchmark_summary["sharpe"],
        "excess_roi_pct": strategy_summary["roi_pct"] - benchmark_summary["roi_pct"],
        "excess_cagr_pct": performance["excess_cagr_pct"],
        "active_days": int((daily["active_positions"] > 0).sum()),
        "avg_active_positions": float(daily["active_positions"].mean()),
        "max_active_positions": int(daily["active_positions"].max()),
        "end_active_positions": int(daily["active_positions"].iloc[-1]),
        "benchmark_tickers": benchmark_tickers,
        "benchmark_min_daily_tickers": performance["benchmark_min_daily_tickers"],
        "benchmark_max_daily_tickers": performance["benchmark_max_daily_tickers"],
        "benchmark_coverage_pct": performance["benchmark_min_daily_tickers"] / benchmark_tickers * 100.0 if benchmark_tickers else 0.0,
        "avg_gross_exposure_pct": float(daily["gross_exposure_pct"].mean()),
        "max_gross_exposure_pct": float(daily["gross_exposure_pct"].max()),
        "avg_net_exposure_pct": float(daily["net_exposure_pct"].mean()),
        "end_cash": float(daily["cash"].iloc[-1]),
        "box_flat_trade_count": flat_stats["count"],
        "box_flat_win_rate_pct": flat_stats["win_rate"],
        "box_flat_nav_contribution_pct": sum(
            float(trade.get("nav_contribution_pct", 0.0))
            for trade in flat_trades
        ),
        "box_trend_trade_count": trend_stats["count"],
        "box_trend_win_rate_pct": trend_stats["win_rate"],
        "box_trend_nav_contribution_pct": sum(
            float(trade.get("nav_contribution_pct", 0.0))
            for trade in trend_trades
        ),
        **target,
        "error": "",
    }


def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def run_analysis(output_dir=None, max_stale_days=15, minimum_trades=30, timestamp=None):
    universe_specs = _universe_specs()
    master_universe = sorted(
        set().union(*(universe_specs[name] for name in BASE_GROUPS))
    )
    print(f"Loading local OHLCV once: {len(master_universe)} unique tickers", flush=True)
    price_data = {
        ticker: core_market_data_store.get_price_data(ticker, period_days=1060)
        for ticker in master_universe
    }
    latest_dates = [df.index.max() for df in price_data.values() if df is not None and not df.empty]
    if not latest_dates:
        raise RuntimeError("No local market data is available")
    cutoff = min(pd.Timestamp(core_backtest_periods.OOS_END), max(latest_dates)).normalize()
    price_data = {
        ticker: (df.loc[:cutoff].copy() if df is not None and not df.empty else df)
        for ticker, df in price_data.items()
    }
    windows = {
        "IS": {
            "evaluation_start": core_backtest_periods.SELECTION_START,
            "evaluation_end": core_backtest_periods.SELECTION_END,
        },
        "OOS": {
            "evaluation_start": core_backtest_periods.OOS_START,
            "evaluation_end": cutoff.date().isoformat(),
        },
    }

    summary_rows, daily_rows = [], []
    for strategy in DIRECTIONAL_STRATEGIES:
        for window_name in WINDOW_ORDER:
            for mode, side in MODE_SIDES.items():
                print(f"{strategy} | {window_name} | {mode}", flush=True)
                ticker_results = {}
                for ticker in master_universe:
                    try:
                        ticker_results[ticker] = _runner(
                            strategy,
                            ticker,
                            price_data[ticker],
                            side,
                            windows[window_name],
                        )
                    except Exception as exc:
                        ticker_results[ticker] = exc
                for universe_name in UNIVERSE_ORDER:
                    universe = universe_specs[universe_name]
                    result = aggregate_cached_results(universe, ticker_results)
                    row = _performance_row(
                        strategy,
                        universe_name,
                        mode,
                        window_name,
                        result,
                        universe,
                        price_data,
                        daily_rows,
                        max_stale_days,
                        minimum_trades,
                    )
                    summary_rows.append(row)

    directory = (
        Path(output_dir)
        if output_dir
        else Path(runtime_settings.EXPORT_DIR)
    )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = directory / f"strategy_universe_direction_{stamp}_summary.csv"
    daily_path = directory / f"strategy_universe_direction_{stamp}_daily_nav.csv"
    metadata_path = directory / f"strategy_universe_direction_{stamp}_metadata.json"
    _write_csv(summary_path, SUMMARY_COLUMNS, summary_rows)
    _write_csv(daily_path, DAILY_COLUMNS, daily_rows)

    stale_tickers = sorted(
        ticker
        for ticker, df in price_data.items()
        if df is None or df.empty or (cutoff - pd.Timestamp(df.index.max()).normalize()).days > max_stale_days
    )
    metadata = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_cutoff": cutoff.date().isoformat(),
        "summary_rows": len(summary_rows),
        "daily_rows": len(daily_rows),
        "unique_tickers": len(master_universe),
        "universes": {name: len(universe_specs[name]) for name in UNIVERSE_ORDER},
        "stale_tickers": stale_tickers,
        "assumptions": {
            "signal": "close-confirmed",
            "entry_execution": core_entry_execution.NEXT_OPEN,
            "neutralization": "NONE",
            "trading_costs": 0,
            "initial_nav": 100.0,
            "max_entry_gross_pct": 100.0,
            "max_security_weight_pct": 5.0,
            "portfolio": "score-weighted fixed quantities with cash and explicit exits",
            "benchmark": "daily equal-weight raw-close universe",
            "minimum_trades": minimum_trades,
            "minimum_win_rate_pct": 58.0,
            "primary_target": "2x same-window benchmark ROI when benchmark ROI is positive",
            "win_rate_target": "58% secondary diagnostic; does not determine PASS/FAIL",
            "ls_definition": "50% long / 50% short entry-side caps; cash is allowed",
            "max_stale_calendar_days": max_stale_days,
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nOOS summary", flush=True)
    for row in sorted(
        (item for item in summary_rows if item["window"] == "OOS"),
        key=lambda item: float(item["strategy_roi_pct"] or -1e9),
        reverse=True,
    ):
        print(
            f"{row['strategy']:<10} | {row['universe']:<16} | {row['mode']:<5} | "
            f"trades {row['trade_count']:>4} | ROI {float(row['strategy_roi_pct'] or 0):+7.2f}% | "
            f"win {float(row['win_rate_pct'] or 0):5.1f}% | {row['status']}",
            flush=True,
        )
    print(f"\nSummary CSV: {summary_path}", flush=True)
    print(f"Daily NAV CSV: {daily_path}", flush=True)
    print(f"Metadata JSON: {metadata_path}", flush=True)
    return summary_rows, daily_rows, metadata, summary_path, daily_path, metadata_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir")
    parser.add_argument("--max-stale-days", type=int, default=15)
    parser.add_argument("--minimum-trades", type=int, default=30)
    parser.add_argument("--timestamp")
    args = parser.parse_args()
    run_analysis(
        output_dir=args.output_dir,
        max_stale_days=args.max_stale_days,
        minimum_trades=args.minimum_trades,
        timestamp=args.timestamp,
    )


if __name__ == "__main__":
    main()
