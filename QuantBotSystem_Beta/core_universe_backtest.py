# File name: universe_backtest.py
# Shared trade-level aggregation for full-universe strategy backtests.
import csv
from datetime import datetime
from pathlib import Path
from statistics import median

import core_cross_sectional_neutralization
import core_market_data_store


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


def _neutralize_trade_entries(trades, eligible_tickers, mode):
    mode = core_cross_sectional_neutralization.normalize_mode(mode)
    if mode == "NONE":
        for trade in trades:
            trade["neutralized_score"] = float(
                trade.get("signal_score", 1.0 if trade.get("side") == "LONG" else -1.0)
            )
        return trades

    metadata = core_market_data_store.get_security_metadata(eligible_tickers)
    cohorts = {}
    for trade in trades:
        cohorts.setdefault(trade.get("entry_date"), []).append(trade)

    selected = []
    for cohort in cohorts.values():
        raw_scores = {ticker: 0.0 for ticker in eligible_tickers}
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


def aggregate_universe_backtest(
    universe_list,
    runner,
    neutralization="NONE",
    **runner_kwargs,
):
    all_trades = []
    eligible_tickers = 0
    failed_tickers = []
    starts, ends = [], []
    eligible_names = []

    for ticker in universe_list:
        try:
            _, _, _, period_label, details = runner(
                ticker,
                include_trades=True,
                **runner_kwargs,
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

    all_trades = _neutralize_trade_entries(
        all_trades, eligible_names, neutralization
    )
    all_trades.sort(key=lambda trade: (trade.get("exit_date") or "", trade["ticker"]))
    long_trades = [trade for trade in all_trades if trade.get("side") == "LONG"]
    short_trades = [trade for trade in all_trades if trade.get("side") == "SHORT"]
    return {
        "universe_count": len(universe_list),
        "eligible_tickers": eligible_tickers,
        "traded_tickers": len({trade["ticker"] for trade in all_trades}),
        "failed_tickers": failed_tickers,
        "period": f"{min(starts)} to {max(ends)}" if starts and ends else "N/A",
        "trades": all_trades,
        "all": _stats(all_trades),
        "long": _stats(long_trades),
        "short": _stats(short_trades),
    }


def _stats_line(label, stats):
    if not stats["count"]:
        return f"{label}: no closed trades"
    return (
        f"{label}: trades {stats['count']} | avg {stats['avg']:+.2f}% | "
        f"median {stats['median']:+.2f}% | win {stats['win_rate']:.1f}% "
        f"({stats['wins']}/{stats['count']})"
    )


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


def export_trade_events_csv(selection_result, oos_result, strategy_key, output_dir=None):
    """Write one normalized row per trade exit so partial exits remain analyzable."""
    safe_key = "".join(char if char.isalnum() or char in "_-" else "_" for char in strategy_key)
    directory = Path(output_dir) if output_dir else Path(__file__).parent / "data" / "backtest_exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe_key}_{datetime.now():%Y%m%d_%H%M%S}_trade_events.csv"
    columns = [
        "window", "evaluation_start", "evaluation_end", "strategy", "trade_id", "ticker", "side",
        "listing_country", "company_country", "sector", "industry",
        "signal_score", "neutralized_score", "entry_execution", "signal_close", "next_open",
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
    metadata_by_ticker = core_market_data_store.get_security_metadata(export_tickers)

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
                        "trade_id": f"{window}-{sequence:05d}",
                        "ticker": trade.get("ticker", ""),
                        "side": side,
                        "listing_country": metadata.get("listing_country") or "",
                        "company_country": metadata.get("company_country") or "",
                        "sector": metadata.get("sector") or "",
                        "industry": metadata.get("industry") or "",
                        "signal_score": trade.get("signal_score", ""),
                        "neutralized_score": trade.get("neutralized_score", ""),
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


def format_universe_backtest(result, entry_text, exit_text):
    report = "\n\nBacktest Summary - Full Universe (1Y)\n"
    report += "-----------------------------------------\n"
    report += f"Entry: {entry_text}\n"
    report += f"Exit: {exit_text}\n"
    report += (
        f"Evaluation: {result['period']} | universe {result['universe_count']} | "
        f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}\n"
    )
    report += _stats_line("ALL", result["all"]) + "\n"
    report += "Use See Detail for rules, LONG/SHORT statistics, and all trade dates.\n"
    report += "-----------------------------------------\n"

    detail = "**Backtest Detail - Full Universe (1Y)**\n"
    detail += "-----------------------------------------\n"
    detail += f"Entry: {entry_text}\nExit: {exit_text}\n"
    detail += (
        "Method: all qualifying signals across the selected universe; no overlap per ticker; "
        "equal weight per completed trade; no cross-ticker capital limit; trading costs excluded.\n"
    )
    detail += (
        f"Evaluation: {result['period']} | universe {result['universe_count']} | "
        f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}\n"
    )
    detail += _stats_line("ALL", result["all"]) + "\n"
    detail += _stats_line("LONG", result["long"]) + "\n"
    detail += _stats_line("SHORT", result["short"]) + "\n"
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
):
    """Format fixed 2024-25 selection evidence and 2026 OOS results together."""
    def result_line(result, include_period=True, separate_all=False):
        period_text = f"{result['period']} | " if include_period else ""
        prefix = (
            f"{period_text}universe {result['universe_count']} | "
            f"data eligible {result['eligible_tickers']} | traded tickers {result['traded_tickers']}"
        )
        separator = "\n" if separate_all else " | "
        return prefix + separator + _stats_line("ALL", result["all"])

    report = "\n\nBacktest Summary\n"
    report += "-----------------------------------------\n"
    report += f"Entry: {entry_text}\n"
    report += f"Exit: {exit_text}\n"
    report += "Candidate selection (IS: 2024-01 to 2025-12):\n"
    report += result_line(selection_result, include_period=False, separate_all=True) + "\n"
    _, oos_latest_date = _period_bounds(oos_result["period"])
    report += f"Fixed OOS (2026-01 ~ {oos_latest_date or 'N/A'}):\n"
    report += result_line(oos_result, include_period=False, separate_all=True) + "\n"
    report += "Use See Detail for LONG/SHORT statistics and the trade-event CSV.\n"
    report += "-----------------------------------------\n"

    detail = "**Backtest Detail - Fixed IS/OOS**\n"
    detail += "-----------------------------------------\n"
    detail += f"Entry: {entry_text}\nExit: {exit_text}\n"
    detail += (
        "Method: all qualifying signals across the selected universe; no overlap per ticker; "
        "equal weight per completed trade; no cross-ticker capital limit; trading costs excluded.\n"
    )
    for label, result in (
        ("Candidate selection (IS: 2024-01 to 2025-12)", selection_result),
        ("Fixed OOS (2026-01 to 2026-07)", oos_result),
    ):
        detail += f"\n{label}\n"
        detail += "Evaluation: " + result_line(result) + "\n"
        detail += _stats_line("LONG", result["long"]) + "\n"
        detail += _stats_line("SHORT", result["short"]) + "\n"
        detail += f"Closed trades: {result['all']['count']}\n"
    detail += "\nSee Detail also sends an Excel-ready CSV with every entry and exit event.\n"
    detail += "-----------------------------------------\n"
    csv_path = (
        export_trade_events_csv(selection_result, oos_result, strategy_key)
        if strategy_key
        else None
    )
    payload = report + DETAIL_MARKER + detail
    return payload + (CSV_MARKER + str(csv_path) if csv_path else "")


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
