# File name: universe_backtest.py
# Shared trade-level aggregation for full-universe strategy backtests.
from statistics import median


DETAIL_MARKER = "\n<<BACKTEST_DETAIL>>\n"


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


def aggregate_universe_backtest(universe_list, runner, **runner_kwargs):
    all_trades = []
    eligible_tickers = 0
    failed_tickers = []
    starts, ends = [], []

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


def split_summary_detail(report):
    if DETAIL_MARKER not in report:
        return report, None
    summary, detail = report.split(DETAIL_MARKER, 1)
    return summary.rstrip(), detail.strip()
