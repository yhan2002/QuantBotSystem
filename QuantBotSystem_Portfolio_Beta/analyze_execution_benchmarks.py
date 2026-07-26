"""Developer-only comparison of entry execution assumptions across Beta strategies."""

import csv
from datetime import datetime
from pathlib import Path

import config
import core_backtest_periods
import core_entry_execution
import core_market_data_store
import core_portfolio_performance
import core_universe_backtest
import strategy_box_range
import strategy_ma_cross
import strategy_price_liquidity_reversion


UNIVERSE_GROUPS = ("CSQ_UNIV", "JP_UNIV", "CH_UNIV")
MODES = (
    core_entry_execution.NEXT_OPEN,
    core_entry_execution.NEXT_DAY_OHLC4,
    core_entry_execution.ADVERSE_GAP_FILTER,
)
WINDOWS = (
    ("IS", core_backtest_periods.selection_window()),
    ("OOS", core_backtest_periods.oos_window()),
)


def _universe():
    return sorted(
        {
            ticker
            for group in UNIVERSE_GROUPS
            for ticker in config.universe_groups[group]
        }
    )


def _mean(trades, field):
    values = [float(trade[field]) for trade in trades if trade.get(field) is not None]
    return sum(values) / len(values) if values else 0.0


def _strategy_runners(price_data):
    def momentum(ticker, **kwargs):
        return strategy_ma_cross.run_isolated_backtest(
            ticker, df=price_data[ticker], **kwargs
        )

    def box(ticker, **kwargs):
        return strategy_box_range.run_box_backtest(
            ticker, df=price_data[ticker], **kwargs
        )

    def reversion(ticker, **kwargs):
        return strategy_price_liquidity_reversion.run_reversion_backtest(
            ticker, df=price_data[ticker], **kwargs
        )

    return (
        ("Momentum", momentum, {"period": core_backtest_periods.HISTORY_PERIOD}),
        ("Box", box, {"period": core_backtest_periods.HISTORY_PERIOD}),
        ("Reversion", reversion, {"period_days": 1060}),
    )


def run_analysis():
    universe = _universe()
    print(f"Loading daily OHLCV once: {len(universe)} tickers", flush=True)
    price_data = {
        ticker: core_market_data_store.get_price_data(ticker, period_days=1060)
        for ticker in universe
    }
    results = []
    aggregate_results = {}
    for strategy_name, runner, runner_kwargs in _strategy_runners(price_data):
        for window_name, window_kwargs in WINDOWS:
            for mode in MODES:
                print(f"{strategy_name} | {window_name} | {mode}", flush=True)
                result = core_universe_backtest.aggregate_universe_backtest(
                    universe,
                    runner,
                    neutralization="NONE",
                    entry_execution=mode,
                    adverse_gap_limit_pct=2.0,
                    **runner_kwargs,
                    **window_kwargs,
                )
                aggregate_results[(strategy_name, window_name, mode)] = result
                trades = result["trades"]
                row = {
                    "strategy": strategy_name,
                    "window": window_name,
                    "execution": mode,
                    "period": result["period"],
                    "universe": result["universe_count"],
                    "eligible_tickers": result["eligible_tickers"],
                    "traded_tickers": result["traded_tickers"],
                    "trades": result["all"]["count"],
                    "avg_trade_return_pct": result["all"]["avg"],
                    "median_trade_return_pct": result["all"]["median"],
                    "win_rate_pct": result["all"]["win_rate"],
                    "avg_signal_to_open_pct": _mean(trades, "signal_to_open_return_pct"),
                    "avg_signal_to_fill_pct": _mean(trades, "signal_to_fill_return_pct"),
                    "portfolio_roi_pct": "",
                    "portfolio_mdd_pct": "",
                    "portfolio_sharpe": "",
                }
                if window_name == "OOS" and trades:
                    performance = core_portfolio_performance.calculate_universe_performance(
                        result,
                        universe,
                        price_loader=lambda ticker: price_data[ticker],
                        max_stale_calendar_days=15,
                    )
                    row.update(
                        portfolio_roi_pct=performance["strategy"]["roi_pct"],
                        portfolio_mdd_pct=performance["strategy"]["mdd_pct"],
                        portfolio_sharpe=performance["strategy"]["sharpe"],
                    )
                results.append(row)

    baseline_counts = {
        (row["strategy"], row["window"]): row["trades"]
        for row in results
        if row["execution"] == core_entry_execution.NEXT_OPEN
    }
    for row in results:
        baseline = baseline_counts[(row["strategy"], row["window"])]
        row["trade_coverage_vs_open_pct"] = (
            row["trades"] / baseline * 100.0 if baseline else 0.0
        )

    output_dir = Path(__file__).parent / "data" / "backtest_exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"execution_benchmarks_{timestamp}.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    detail_path = output_dir / f"execution_benchmark_trades_{timestamp}.csv"
    detail_columns = [
        "strategy", "window", "execution", "ticker", "side", "entry_date",
        "signal_close", "next_open", "entry_price", "signal_to_open_return_pct",
        "signal_to_fill_return_pct", "exit_date", "trade_return_pct",
    ]
    with detail_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=detail_columns)
        writer.writeheader()
        for (strategy_name, window_name, mode), result in aggregate_results.items():
            for trade in result["trades"]:
                events = trade.get("exit_events") or []
                exit_date = (
                    events[-1].get("date")
                    if events
                    else trade.get("exit_date", "")
                )
                writer.writerow(
                    {
                        "strategy": strategy_name,
                        "window": window_name,
                        "execution": mode,
                        "ticker": trade.get("ticker", ""),
                        "side": trade.get("side", ""),
                        "entry_date": trade.get("entry_date", ""),
                        "signal_close": trade.get("signal_close", ""),
                        "next_open": trade.get("next_open", ""),
                        "entry_price": trade.get("entry_price", ""),
                        "signal_to_open_return_pct": trade.get("signal_to_open_return_pct", ""),
                        "signal_to_fill_return_pct": trade.get("signal_to_fill_return_pct", ""),
                        "exit_date": exit_date,
                        "trade_return_pct": trade.get("return", ""),
                    }
                )

    print("\nOOS summary", flush=True)
    for row in results:
        if row["window"] != "OOS":
            continue
        print(
            f"{row['strategy']:<9} | {row['execution']:<18} | "
            f"trades {row['trades']:>4} | avg {row['avg_trade_return_pct']:+.2f}% | "
            f"win {row['win_rate_pct']:.1f}% | ROI {row['portfolio_roi_pct']:+.2f}% | "
            f"MDD {row['portfolio_mdd_pct']:+.2f}% | Sharpe {row['portfolio_sharpe']:+.2f}",
            flush=True,
        )
    print(f"\nCSV: {output_path}", flush=True)
    print(f"Trade CSV: {detail_path}", flush=True)
    return results, output_path, detail_path, aggregate_results


if __name__ == "__main__":
    run_analysis()
