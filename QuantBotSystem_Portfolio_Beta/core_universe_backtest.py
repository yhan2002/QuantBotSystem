# File name: universe_backtest.py
# Shared trade-level aggregation for full-universe strategy backtests.
import csv
import copy
from datetime import datetime
import json
import zipfile
from pathlib import Path
from statistics import median

import pandas as pd

import core_cross_sectional_neutralization
import core_backtest_periods
import core_market_data_store
import core_portfolio_performance
import core_signal_scoring
import runtime_settings


DETAIL_MARKER = "\n<<BACKTEST_DETAIL>>\n"
CSV_MARKER = "\n<<BACKTEST_CSV>>\n"


def _period_bounds(label):
    if not label or label == "N/A" or " to " not in label:
        return None, None
    start, end = label.split(" to ", 1)
    return start, end


def _trade_exit_date(trade):
    if trade.get("exit_date"):
        return trade["exit_date"]
    events = trade.get("exit_events") or []
    return events[-1].get("date") if events else None


def _stats(trades):
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


def _price_availability_bounds(tickers, price_loader):
    """Return first/last observed price dates without using future membership."""
    bounds = {}
    for ticker in sorted(set(tickers)):
        prices = price_loader(ticker)
        if prices is None or prices.empty:
            continue
        dates = pd.DatetimeIndex(
            pd.to_datetime(prices.index, errors="coerce")
        ).dropna()
        if dates.empty:
            continue
        if dates.tz is not None:
            dates = dates.tz_localize(None)
        dates = dates.normalize()
        bounds[ticker] = (dates.min(), dates.max())
    return bounds


def _box_attribution(trades):
    buckets = {
        "FLAT": [],
        "TREND": [],
        "RISING": [],
        "FALLING": [],
    }
    for trade in trades:
        structure = str(trade.get("structure") or "").upper()
        if structure == "FLAT":
            buckets["FLAT"].append(trade)
        elif structure in ("RISING", "FALLING"):
            buckets["TREND"].append(trade)
            buckets[structure].append(trade)
    result = {}
    for label, rows in buckets.items():
        stats = _stats(rows)
        stats["nav_contribution_pct"] = sum(
            float(row.get("nav_contribution_pct", 0.0)) for row in rows
        )
        stats["allocated_gross_notional"] = sum(
            float(row.get("allocated_gross_notional", 0.0)) for row in rows
        )
        result[label] = stats
    return result


def _neutralize_trade_entries(
    trades,
    eligible_tickers,
    mode,
    price_availability_loader=None,
):
    mode = core_cross_sectional_neutralization.normalize_mode(mode)
    if mode == "NONE":
        for trade in trades:
            trade["neutralized_score"] = float(
                trade.get("signal_score", 1.0 if trade.get("side") == "LONG" else -1.0)
            )
        return trades

    eligible_tickers = list(dict.fromkeys(eligible_tickers))
    metadata = core_market_data_store.get_security_metadata(eligible_tickers)
    price_loader = (
        price_availability_loader
        if price_availability_loader is not None
        else core_market_data_store.get_price_data
    )
    availability = _price_availability_bounds(eligible_tickers, price_loader)
    cohorts = {}
    for trade in trades:
        cohorts.setdefault(trade.get("entry_date"), []).append(trade)

    selected = []
    for entry_date, cohort in cohorts.items():
        cohort_date = pd.Timestamp(entry_date)
        if cohort_date.tzinfo is not None:
            cohort_date = cohort_date.tz_localize(None)
        cohort_date = cohort_date.normalize()
        available_tickers = {
            ticker
            for ticker, (first_date, last_date) in availability.items()
            if first_date <= cohort_date <= last_date
        }
        # A recorded trade proves that its security was executable even if a
        # custom availability loader omitted it. Non-signal securities remain
        # zero-score members only while their price history is actually live.
        available_tickers.update(trade["ticker"] for trade in cohort)
        raw_scores = {ticker: 0.0 for ticker in sorted(available_tickers)}
        for trade in cohort:
            raw_scores[trade["ticker"]] = float(
                trade.get("signal_score", 1.0 if trade.get("side") == "LONG" else -1.0)
            )
        neutralized = core_cross_sectional_neutralization.neutralize_scores(
            raw_scores, mode, metadata
        )
        for trade in cohort:
            score = neutralized.get(trade["ticker"], 0.0)
            trade["neutralized_score"] = score
            if (trade.get("side") == "LONG" and score > 0) or (
                trade.get("side") == "SHORT" and score < 0
            ):
                selected.append(trade)
    return selected


def _notify_progress(progress_callback, completed, total, ticker, succeeded):
    """Report ticker completion without letting UI failures affect results."""
    if progress_callback is None:
        return
    try:
        progress_callback(completed, total, ticker, succeeded)
    except Exception:
        # Progress reporting is best-effort. Telegram/API/UI failures must not
        # turn a successfully evaluated security into a failed backtest row.
        pass


def _latest_usable_market_date(
    requested_end,
    price_loader,
    max_stale_calendar_days=core_backtest_periods.BENCHMARK_MAX_STALE_CALENDAR_DAYS,
):
    """Return the latest usable ACWI date without silently using stale data."""
    if not requested_end:
        return None
    requested = pd.Timestamp(requested_end).normalize()
    staleness_reference = min(requested, pd.Timestamp.now().normalize())
    try:
        prices = price_loader(core_portfolio_performance.MARKET_BENCHMARK_TICKER)
    except Exception:
        return None
    if prices is None or prices.empty:
        return None
    for column in ("Adj Close", "Close"):
        if column not in prices:
            continue
        series = pd.to_numeric(prices[column], errors="coerce")
        series = series.replace([float("inf"), float("-inf")], pd.NA).dropna()
        series = series[series > 0]
        if series.empty:
            continue
        series.index = pd.to_datetime(series.index).normalize()
        candidates = series.index[series.index <= staleness_reference]
        if candidates.empty:
            continue
        latest = candidates[-1]
        if (staleness_reference - latest).days <= max_stale_calendar_days:
            return latest.date().isoformat()
    return None


def aggregate_universe_backtest(
    universe_list,
    runner,
    neutralization="NONE",
    position_mode="LONG_SHORT_NEUTRAL",
    portfolio_accounting=False,
    portfolio_policy=None,
    signal_score_overlay=None,
    price_availability_loader=None,
    market_benchmark_loader=None,
    progress_callback=None,
    **runner_kwargs,
):
    """Aggregate ticker backtests and optionally report per-ticker completion.

    ``progress_callback`` receives ``(completed, total, ticker, succeeded)``
    exactly once after each ticker attempt. Callback exceptions are ignored so
    presentation-layer failures cannot alter trades, eligibility, or NAV.
    """
    all_trades = []
    eligible_tickers = 0
    failed_tickers = []
    starts, ends = [], []
    eligible_names = []
    total_tickers = len(universe_list)
    benchmark_loader = (
        market_benchmark_loader or core_market_data_store.get_price_data
    )
    effective_runner_kwargs = dict(runner_kwargs)
    if portfolio_accounting:
        common_end = _latest_usable_market_date(
            effective_runner_kwargs.get("evaluation_end"),
            benchmark_loader,
        )
        if common_end:
            effective_runner_kwargs["evaluation_end"] = common_end

    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            try:
                _, _, _, period_label, details = runner(
                    ticker,
                    include_trades=True,
                    **effective_runner_kwargs,
                )
            except Exception:
                failed_tickers.append(ticker)
                continue

            start, end = _period_bounds(period_label)
            if start and end:
                eligible_tickers += 1
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
            succeeded = bool(start and end)
        finally:
            _notify_progress(
                progress_callback,
                completed,
                total_tickers,
                ticker,
                succeeded,
            )

    if signal_score_overlay is not None:
        all_trades = signal_score_overlay(all_trades, eligible_names)

    all_trades = _neutralize_trade_entries(
        all_trades,
        eligible_names,
        neutralization,
        price_availability_loader=price_availability_loader,
    )
    all_trades = core_signal_scoring.annotate_trades_by_entry_date(all_trades)
    all_trades.sort(key=lambda trade: (trade.get("exit_date") or "", trade["ticker"]))
    long_trades = [trade for trade in all_trades if trade.get("side") == "LONG"]
    short_trades = [trade for trade in all_trades if trade.get("side") == "SHORT"]
    result = {
        "universe_count": len(universe_list),
        "eligible_tickers": eligible_tickers,
        "traded_tickers": len({trade["ticker"] for trade in all_trades}),
        "failed_tickers": failed_tickers,
        "period": f"{min(starts)} to {max(ends)}" if starts and ends else "N/A",
        "trades": all_trades,
        "all": _stats(all_trades),
        "long": _stats(long_trades),
        "short": _stats(short_trades),
        "box_attribution": _box_attribution(all_trades),
    }
    if not portfolio_accounting or result["period"] == "N/A":
        return result

    start_date, end_date = _period_bounds(result["period"])
    performance = core_portfolio_performance.calculate_score_weighted_performance(
        all_trades,
        universe_list,
        start_date,
        end_date,
        position_mode=position_mode,
        policy=portfolio_policy,
        market_benchmark_loader=benchmark_loader,
    )
    qualified_trades = list(all_trades)
    allocated_rows = performance["ledger"]["trades"]
    executed_trades = [
        trade
        for trade in allocated_rows
        if trade.get("allocation_status") == "FILLED"
    ]
    skipped_trades = [
        trade
        for trade in allocated_rows
        if trade.get("allocation_status") != "FILLED"
    ]
    result.update(
        {
            "qualified_trades": qualified_trades,
            "qualified_all": _stats(qualified_trades),
            "trades": executed_trades,
            "skipped_trades": skipped_trades,
            "traded_tickers": len(
                {trade["ticker"] for trade in executed_trades}
            ),
            "all": _stats(executed_trades),
            "long": _stats(
                [
                    trade
                    for trade in executed_trades
                    if trade.get("side") == "LONG"
                ]
            ),
            "short": _stats(
                [
                    trade
                    for trade in executed_trades
                    if trade.get("side") == "SHORT"
                ]
            ),
            "box_attribution": _box_attribution(executed_trades),
            "portfolio_performance": performance,
        }
    )
    return result


def _stats_line(label, stats):
    if not stats["count"]:
        return f"{label}: no closed trades"
    return (
        f"{label}: trades {stats['count']} | avg trade {stats['avg']:+.2f}% | "
        f"median {stats['median']:+.2f}% | win {stats['win_rate']:.1f}% "
        f"({stats['wins']}/{stats['count']})"
    )


def _portfolio_cagr_line(label, result):
    """Use actual ledger NAV for the headline return when it is available."""
    performance = result.get("portfolio_performance")
    if not performance:
        return _stats_line(label, result["all"])

    stats = result["all"]
    cagr = performance["strategy"].get("cagr_pct")
    cagr_text = "N/A" if cagr is None else f"{float(cagr):+.2f}%"
    win_text = (
        f"{stats['win_rate']:.1f}% ({stats['wins']}/{stats['count']})"
        if stats["count"]
        else "N/A (0/0)"
    )
    return (
        f"{label}: trades {stats['count']} | portfolio CAGR {cagr_text} | "
        f"trade win {win_text}"
    )


def _portfolio_lines(result):
    performance = result.get("portfolio_performance")
    if not performance:
        return ["Portfolio accounting: unavailable"]
    strategy = performance["strategy"]
    market = performance.get("market_benchmark", performance.get("benchmark", {}))
    universe_ew = performance.get(
        "universe_ew_benchmark", performance.get("benchmark", {})
    )
    daily = performance["daily"]
    ledger = performance["ledger"]
    policy = ledger["policy"]
    max_gross = (
        float(daily["gross_exposure_pct"].max()) if not daily.empty else 0.0
    )
    average_gross = (
        float(daily["gross_exposure_pct"].mean()) if not daily.empty else 0.0
    )
    performance_parts = []
    if strategy["cagr_pct"] is not None:
        performance_parts.append(f"CAGR {strategy['cagr_pct']:+.2f}%")
    performance_parts.append(f"MDD {strategy['mdd_pct']:+.2f}%")
    if strategy["sharpe"] is not None:
        performance_parts.append(f"Sharpe {strategy['sharpe']:.2f}")
    market_roi = market.get("roi_pct")
    universe_roi = universe_ew.get("roi_pct")
    strategy_roi = strategy.get("roi_pct")
    multiple = performance.get("market_benchmark_multiple")
    if (
        multiple is None
        and market_roi is not None
        and float(market_roi) > 0
        and strategy_roi is not None
    ):
        multiple = float(strategy_roi) / float(market_roi)
    market_text = "N/A" if market_roi is None else f"{float(market_roi):+.1f}%"
    strategy_text = (
        "N/A" if strategy_roi is None else f"{float(strategy_roi):+.1f}%"
    )
    multiple_text = "N/A" if multiple is None else f"{float(multiple):.2f}x"
    market_label = performance.get("market_benchmark_label", "ACWI")
    universe_text = (
        "N/A" if universe_roi is None else f"{float(universe_roi):+.1f}%"
    )
    stock_selection_excess = performance.get("stock_selection_excess_pct")
    if (
        stock_selection_excess is None
        and strategy_roi is not None
        and universe_roi is not None
    ):
        stock_selection_excess = float(strategy_roi) - float(universe_roi)
    stock_selection_text = (
        "N/A"
        if stock_selection_excess is None
        else f"{float(stock_selection_excess):+.1f}%p"
    )
    lines = [
        (
            f"{market_label} ROI: {market_text} | Strategy ROI: {strategy_text} | "
            f"Multiple: {multiple_text}"
        ),
        (
            f"Universe EW ROI: {universe_text} | "
            f"Stock-selection excess: {stock_selection_text}"
        ),
        "Portfolio risk: " + " | ".join(performance_parts),
        (
            "Execution: "
            f"filled {ledger['filled_trades']} | skipped {ledger['skipped_trades']} | "
            f"evaluation-end marks {ledger.get('evaluation_end_marks', 0)}"
        ),
        (
            f"Exposure: avg gross {average_gross:.1f}% | max gross {max_gross:.1f}% | "
            f"entry caps gross {policy['max_gross'] * 100:.0f}%, "
            f"name {policy['max_name_weight'] * 100:.0f}%, "
            f"positions {policy.get('max_positions', 30)}, "
            f"long {policy['long_cap'] * 100:.0f}%, "
            f"short {policy['short_cap'] * 100:.0f}%"
        ),
    ]
    if market_roi is None:
        reason = performance.get("market_benchmark_unavailable_reason")
        lines.insert(
            2,
            "Benchmark status: REVIEW"
            + (f" | {reason}" if reason else ""),
        )
    return lines


def _box_attribution_lines(result):
    attribution = result.get("box_attribution") or {}
    lines = []
    for label in ("FLAT", "TREND", "RISING", "FALLING"):
        stats = attribution.get(label)
        if stats and stats.get("count"):
            lines.append(
                _stats_line(f"BOX {label}", stats)
                + f" | NAV contribution {stats['nav_contribution_pct']:+.2f}%"
            )
    return lines


def _trade_line(trade):
    entry = trade.get("entry_date", "N/A")
    exit_date = trade.get("exit_date") or "N/A"
    events = trade.get("exit_events") or []
    if events:
        exit_text = ", ".join(
            f"{event.get('date', 'N/A')} ({event.get('reason', 'exit')})"
            for event in events
        )
    else:
        exit_text = exit_date
    return (
        f"- {trade.get('side', 'N/A')} {trade['ticker']}: "
        f"{entry} -> {exit_text} {float(trade['return']):+.1f}%"
    )


def export_trade_events_csv(
    selection_result,
    oos_result,
    strategy_key,
    output_dir=None,
    metadata_by_ticker=None,
):
    """Write one normalized row per trade exit so partial exits remain analyzable."""
    safe_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in strategy_key)
    directory = (
        Path(output_dir)
        if output_dir
        else Path(runtime_settings.EXPORT_DIR)
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        f"{safe_key}_{datetime.now():%Y%m%d_%H%M%S_%f}_trade_events.csv"
    )
    columns = [
        "window", "evaluation_start", "evaluation_end", "strategy", "trade_id", "ticker", "side",
        "listing_country", "company_country", "sector", "industry",
        "setup", "structure", "flat_slope_margin", "trend_slope_margin",
        "trend_efficiency_margin", "lower_touches", "upper_touches",
        "signal_score", "neutralized_score", "selected_score",
        "winsorized_score", "percentile_rank", "rank_score", "requested_weight_pct",
        "allocation_status", "evaluation_status", "original_exit_date",
        "evaluation_mark_date", "evaluation_mark_price",
        "evaluated_return_pct", "entry_weight_pct", "allocated_base_notional",
        "allocated_gross_notional", "quantity", "realized_pnl",
        "nav_contribution_pct", "legs_json",
        "entry_execution", "signal_close", "next_open",
        "signal_to_open_return_pct", "signal_to_fill_return_pct", "entry_date", "entry_price",
        "exit_date", "exit_reason", "exit_weight", "exit_price",
        "event_return_pct", "trade_return_pct",
    ]
    export_tickers = {
        trade.get("ticker")
        for result in (selection_result, oos_result)
        for trade in result["trades"]
        if trade.get("ticker")
    }
    if metadata_by_ticker is None:
        metadata_by_ticker = core_market_data_store.get_security_metadata(
            export_tickers
        )

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for window, result in (("IS", selection_result), ("OOS", oos_result)):
            period_start, period_end = _period_bounds(result["period"])
            for sequence, trade in enumerate(result["trades"], start=1):
                metadata = metadata_by_ticker.get(trade.get("ticker"), {})
                events = trade.get("exit_events") or [{
                    "date": trade.get("exit_date"),
                    "reason": "scheduled exit",
                    "weight": 1.0,
                    "price": trade.get("exit_price"),
                }]
                entry_price = float(trade.get("entry_price") or 0.0)
                side = trade.get("side", "N/A")
                for event in events:
                    exit_price = event.get("price")
                    if entry_price > 0 and exit_price is not None:
                        raw_return = (float(exit_price) / entry_price - 1.0) * 100.0
                        event_return = raw_return if side == "LONG" else -raw_return
                    else:
                        event_return = None
                    writer.writerow({
                        "window": window,
                        "evaluation_start": period_start or "",
                        "evaluation_end": period_end or "",
                        "strategy": strategy_key,
                        "trade_id": trade.get(
                            "trade_id", f"{window}-{sequence:05d}"
                        ),
                        "ticker": trade.get("ticker", ""),
                        "side": side,
                        "listing_country": metadata.get("listing_country") or "",
                        "company_country": metadata.get("company_country") or "",
                        "sector": metadata.get("sector") or "",
                        "industry": metadata.get("industry") or "",
                        "setup": trade.get("setup", ""),
                        "structure": trade.get("structure", ""),
                        "flat_slope_margin": trade.get("flat_slope_margin", ""),
                        "trend_slope_margin": trade.get("trend_slope_margin", ""),
                        "trend_efficiency_margin": trade.get(
                            "trend_efficiency_margin", ""
                        ),
                        "lower_touches": trade.get("lower_touches", ""),
                        "upper_touches": trade.get("upper_touches", ""),
                        "signal_score": trade.get("signal_score", ""),
                        "neutralized_score": trade.get("neutralized_score", ""),
                        "selected_score": trade.get("selected_score", ""),
                        "winsorized_score": trade.get("winsorized_score", ""),
                        "percentile_rank": trade.get("percentile_rank", ""),
                        "rank_score": trade.get("rank_score", ""),
                        "requested_weight_pct": (
                            float(trade.get("requested_weight", 0.0)) * 100.0
                        ),
                        "allocation_status": trade.get("allocation_status", ""),
                        "evaluation_status": trade.get("evaluation_status", ""),
                        "original_exit_date": trade.get("original_exit_date", ""),
                        "evaluation_mark_date": trade.get(
                            "evaluation_mark_date", ""
                        ),
                        "evaluation_mark_price": trade.get(
                            "evaluation_mark_price", ""
                        ),
                        "evaluated_return_pct": trade.get(
                            "evaluated_return_pct", ""
                        ),
                        "entry_weight_pct": trade.get("entry_weight_pct", ""),
                        "allocated_base_notional": trade.get(
                            "allocated_base_notional", ""
                        ),
                        "allocated_gross_notional": trade.get(
                            "allocated_gross_notional", ""
                        ),
                        "quantity": trade.get("quantity", ""),
                        "realized_pnl": trade.get("realized_pnl", ""),
                        "nav_contribution_pct": trade.get(
                            "nav_contribution_pct", ""
                        ),
                        "legs_json": (
                            json.dumps(
                                trade.get("legs"),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            if trade.get("legs")
                            else ""
                        ),
                        "entry_execution": trade.get("entry_execution", "NEXT_OPEN"),
                        "signal_close": trade.get("signal_close", ""),
                        "next_open": trade.get("next_open", ""),
                        "signal_to_open_return_pct": trade.get("signal_to_open_return_pct", ""),
                        "signal_to_fill_return_pct": trade.get("signal_to_fill_return_pct", ""),
                        "entry_date": trade.get("entry_date", ""),
                        "entry_price": entry_price or "",
                        "exit_date": event.get("date") or "",
                        "exit_reason": event.get("reason", "scheduled exit"),
                        "exit_weight": event.get("weight", 1.0),
                        "exit_price": exit_price if exit_price is not None else "",
                        "event_return_pct": "" if event_return is None else round(event_return, 6),
                        "trade_return_pct": round(float(trade["return"]), 6),
                    })
    return path


def export_portfolio_bundle(
    selection_result,
    oos_result,
    strategy_key,
    output_dir=None,
    metadata_by_ticker=None,
):
    """Export trades, per-leg ledger events, and daily NAV in one ZIP."""
    directory = (
        Path(output_dir)
        if output_dir
        else Path(runtime_settings.EXPORT_DIR)
    )
    directory.mkdir(parents=True, exist_ok=True)
    safe_key = "".join(
        char if char.isalnum() or char in "_-" else "_"
        for char in strategy_key
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    trade_path = export_trade_events_csv(
        selection_result,
        oos_result,
        strategy_key,
        output_dir=directory,
        metadata_by_ticker=metadata_by_ticker,
    )
    daily_path = directory / f"{safe_key}_{stamp}_portfolio_daily.csv"
    event_path = directory / f"{safe_key}_{stamp}_ledger_events.csv"
    benchmark_path = directory / f"{safe_key}_{stamp}_benchmark_summary.csv"
    bundle_path = directory / f"{safe_key}_{stamp}_portfolio_bundle.zip"

    daily_frames = []
    ledger_rows = []
    benchmark_rows = []
    for window, result in (("IS", selection_result), ("OOS", oos_result)):
        performance = result.get("portfolio_performance")
        if not performance:
            continue
        daily = performance["daily"].copy()
        daily.insert(0, "window", window)
        daily.index.name = "date"
        daily_frames.append(daily.reset_index())
        for event in performance["ledger"]["events"]:
            row = dict(event)
            row["window"] = window
            ledger_rows.append(row)
        market = performance.get("market_benchmark", {})
        universe_ew = performance.get("universe_ew_benchmark", {})
        benchmark_rows.append(
            {
                "window": window,
                "market_benchmark_label": performance.get(
                    "market_benchmark_label", "ACWI"
                ),
                "market_benchmark_ticker": performance.get(
                    "market_benchmark_ticker", "ACWI"
                ),
                "market_benchmark_available": performance.get(
                    "market_benchmark_available", False
                ),
                "market_benchmark_unavailable_reason": performance.get(
                    "market_benchmark_unavailable_reason", ""
                ),
                "start_level_date": performance.get(
                    "market_benchmark_start_level_date", ""
                ),
                "start_level": performance.get(
                    "market_benchmark_start_level", ""
                ),
                "end_level_date": performance.get(
                    "market_benchmark_end_level_date", ""
                ),
                "end_level": performance.get(
                    "market_benchmark_end_level", ""
                ),
                "price_column": performance.get(
                    "market_benchmark_price_column", ""
                ),
                "msci_acwi_roi_pct": market.get("roi_pct"),
                "strategy_roi_pct": performance.get("strategy", {}).get(
                    "roi_pct"
                ),
                "market_multiple": performance.get(
                    "market_benchmark_multiple"
                ),
                "universe_ew_roi_pct": universe_ew.get("roi_pct"),
                "stock_selection_excess_pct": performance.get(
                    "stock_selection_excess_pct"
                ),
                "daily_curve_available": performance.get(
                    "market_benchmark_daily_curve_available", False
                ),
                "daily_curve_reason": performance.get(
                    "market_benchmark_daily_curve_reason", ""
                ),
            }
        )

    if daily_frames:
        pd.concat(daily_frames, ignore_index=True).to_csv(
            daily_path,
            index=False,
            encoding="utf-8-sig",
        )
    else:
        pd.DataFrame(columns=["window", "date"]).to_csv(
            daily_path,
            index=False,
            encoding="utf-8-sig",
        )
    pd.DataFrame(ledger_rows).to_csv(
        event_path,
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(benchmark_rows).to_csv(
        benchmark_path,
        index=False,
        encoding="utf-8-sig",
    )

    with zipfile.ZipFile(
        bundle_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in (trade_path, daily_path, event_path, benchmark_path):
            archive.write(path, arcname=path.name)
    return bundle_path


def _snapshot_export_result(result):
    """Keep only the immutable fields required to build a later export."""
    snapshot = {
        "period": result.get("period", "N/A"),
        "trades": copy.deepcopy(result.get("trades") or []),
    }
    performance = result.get("portfolio_performance")
    if not performance:
        return snapshot

    performance_snapshot = {
        key: copy.deepcopy(value)
        for key, value in performance.items()
        if key not in {"daily", "ledger"}
    }
    daily = performance.get("daily")
    performance_snapshot["daily"] = (
        daily.copy(deep=True)
        if isinstance(daily, pd.DataFrame)
        else pd.DataFrame()
    )
    ledger = performance.get("ledger") or {}
    performance_snapshot["ledger"] = {
        "events": copy.deepcopy(ledger.get("events") or []),
    }
    snapshot["portfolio_performance"] = performance_snapshot
    return snapshot


def build_backtest_export_spec(
    selection_result,
    oos_result,
    strategy_key,
):
    """Capture one scan's export inputs without writing a file."""
    selection_snapshot = _snapshot_export_result(selection_result)
    oos_snapshot = _snapshot_export_result(oos_result)
    export_tickers = {
        trade.get("ticker")
        for result in (selection_snapshot, oos_snapshot)
        for trade in result["trades"]
        if trade.get("ticker")
    }
    return {
        "version": 1,
        "strategy_key": str(strategy_key),
        "selection_result": selection_snapshot,
        "oos_result": oos_snapshot,
        "metadata_by_ticker": copy.deepcopy(
            core_market_data_store.get_security_metadata(export_tickers)
        ),
        "portfolio_bundle": any(
            result.get("portfolio_performance")
            for result in (selection_snapshot, oos_snapshot)
        ),
    }


def materialize_backtest_export(export_spec, output_dir):
    """Create an export only after the user explicitly requests it."""
    if not isinstance(export_spec, dict) or export_spec.get("version") != 1:
        raise ValueError("Unsupported backtest export specification")
    strategy_key = export_spec.get("strategy_key")
    selection_result = export_spec.get("selection_result")
    oos_result = export_spec.get("oos_result")
    if not strategy_key or not selection_result or not oos_result:
        raise ValueError("Incomplete backtest export specification")

    export_kwargs = {
        "output_dir": output_dir,
        "metadata_by_ticker": export_spec.get("metadata_by_ticker") or {},
    }
    if export_spec.get("portfolio_bundle"):
        return export_portfolio_bundle(
            selection_result,
            oos_result,
            strategy_key,
            **export_kwargs,
        )
    return export_trade_events_csv(
        selection_result,
        oos_result,
        strategy_key,
        **export_kwargs,
    )


def format_universe_backtest(result, entry_text, exit_text):
    report = "\n\nBacktest Summary - Full Universe (1Y)\n"
    report += "-----------------------------------------\n"
    report += f"Entry: {entry_text}\n"
    report += f"Exit: {exit_text}\n"
    report += (
        f"Evaluation: {result['period']} | universe {result['universe_count']} | "
        f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}\n"
    )
    report += _portfolio_cagr_line("ALL", result) + "\n"
    if result.get("portfolio_performance"):
        report += "CAGR: annualized from daily portfolio NAV using 252 trading days.\n"
    report += "Use See Detail for rules, LONG/SHORT statistics, and all trade dates.\n"
    report += "-----------------------------------------\n"

    detail = "**Backtest Detail - Full Universe (1Y)**\n"
    detail += "-----------------------------------------\n"
    detail += f"Entry: {entry_text}\nExit: {exit_text}\n"
    detail += (
        "Method: all qualifying signals across the selected universe; no overlap per ticker; "
        "score-proportional entry size; fixed quantities until explicit exits; "
        "cash and gross/side/name limits enforced; trading costs excluded; "
        "CAGR annualized from daily portfolio NAV using 252 trading days.\n"
    )
    detail += (
        f"Evaluation: {result['period']} | universe {result['universe_count']} | "
        f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}\n"
    )
    detail += _portfolio_cagr_line("ALL", result) + "\n"
    detail += _stats_line("LONG", result["long"]) + "\n"
    detail += _stats_line("SHORT", result["short"]) + "\n"
    for line in _portfolio_lines(result):
        detail += line + "\n"
    for line in _box_attribution_lines(result):
        detail += line + "\n"
    if result["trades"]:
        detail += "\nCompleted Trades (chronological)\n"
        detail += "\n".join(_trade_line(trade) for trade in result["trades"]) + "\n"
    else:
        detail += "\nNo closed trades in the evaluation window.\n"
    detail += "-----------------------------------------\n"
    return report + DETAIL_MARKER + detail


def format_fixed_is_oos_backtest(
    selection_result,
    oos_result,
    entry_text,
    exit_text,
    strategy_key=None,
    report_artifacts=None,
):
    """Format fixed 2024-25 selection evidence and 2026 OOS results together."""
    def result_line(result, include_period=True, separate_all=False):
        period_text = f"{result['period']} | " if include_period else ""
        prefix = (
            f"{period_text}universe {result['universe_count']} | "
            f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}"
        )
        separator = "\n" if separate_all else " | "
        return prefix + separator + _portfolio_cagr_line("ALL", result)

    report = "\n\nBacktest Summary\n"
    report += "-----------------------------------------\n"
    report += f"Entry: {entry_text}\n"
    report += f"Exit: {exit_text}\n"
    report += "Candidate selection (IS: 2024-01 to 2025-12):\n"
    report += result_line(selection_result, include_period=False, separate_all=True) + "\n"
    _, oos_latest_date = _period_bounds(oos_result["period"])
    report += f"Fixed OOS (2026-01 ~ {oos_latest_date or 'N/A'}):\n"
    report += result_line(oos_result, include_period=False, separate_all=True) + "\n"
    if any(
        result.get("portfolio_performance")
        for result in (selection_result, oos_result)
    ):
        report += "CAGR: annualized from daily portfolio NAV using 252 trading days.\n"
    report += (
        "Use See Detail for LONG/SHORT statistics. "
        "Use Download Export for analysis files.\n"
    )
    report += "-----------------------------------------\n"

    detail = "**Backtest Detail - Fixed IS/OOS**\n"
    detail += "-----------------------------------------\n"
    detail += f"Entry: {entry_text}\nExit: {exit_text}\n"
    detail += (
        "Method: all qualifying signals across the selected universe; no overlap per ticker; "
        "score-proportional entry size; fixed quantities until explicit exits; "
        "cash and gross/side/name limits enforced; trading costs excluded; "
        "CAGR annualized from daily portfolio NAV using 252 trading days.\n"
    )
    for label, result in (
        ("Candidate selection (IS: 2024-01 to 2025-12)", selection_result),
        ("Fixed OOS (2026-01 to 2026-07)", oos_result),
    ):
        detail += f"\n{label}\n"
        detail += "Evaluation: " + result_line(result) + "\n"
        detail += _stats_line("LONG", result["long"]) + "\n"
        detail += _stats_line("SHORT", result["short"]) + "\n"
        for line in _portfolio_lines(result):
            detail += line + "\n"
        for line in _box_attribution_lines(result):
            detail += line + "\n"
        mark_count = int(
            (result.get("portfolio_performance") or {})
            .get("ledger", {})
            .get("evaluation_end_marks", 0)
        )
        closed_count = max(0, int(result["all"]["count"]) - mark_count)
        detail += (
            f"Closed trades: {closed_count} | "
            f"evaluation-end marks: {mark_count}\n"
        )
    detail += (
        "\nDownload Export creates an analysis-ready file only when requested. "
        "Portfolio-accounted runs include trade events, per-leg ledger events, "
        "daily NAV, and benchmark boundary levels in a ZIP.\n"
    )
    detail += "-----------------------------------------\n"
    if strategy_key and report_artifacts is not None:
        report_artifacts["backtest_export"] = build_backtest_export_spec(
            selection_result,
            oos_result,
            strategy_key,
        )
    return report + DETAIL_MARKER + detail


def split_summary_detail(report):
    summary, detail, _ = split_report_payload(report)
    return summary, detail


def split_report_payload(report):
    csv_path = None
    if CSV_MARKER in report:
        report, csv_section = report.rsplit(CSV_MARKER, 1)
        csv_path, separator, trailing_text = csv_section.partition("\n")
        if separator and trailing_text:
            # Strategy reports may append a visual footer or data warnings after the
            # shared backtest payload. Keep those in the text report, not the path.
            report += "\n" + trailing_text
    if DETAIL_MARKER not in report:
        return report, None, csv_path.strip() if csv_path else None
    summary, detail = report.split(DETAIL_MARKER, 1)
    return summary.rstrip(), detail.strip(), csv_path.strip() if csv_path else None
