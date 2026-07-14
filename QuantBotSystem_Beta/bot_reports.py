# File name: bot_reports.py
# Generates strategy scan, quick-check, and box backtest reports.
import ma_cross_engine
import box_range_engine
import market_data_store
import value_reversion_engine
import local_settings

from bot_session import STRATEGY_LABELS, compile_selected_universe, get_or_create_session


def _should_hide_backtest_detail(chat_id):
    return str(chat_id) in local_settings.hidden_backtest_chat_ids()


def _wins_text(win_rate, count):
    wins = round((win_rate / 100.0) * count) if count else 0
    return f"{win_rate:.1f}% ({wins}/{count})"


def _bt_line(label, avg_return, win_rate, count, side_label):
    side_text = f" | {side_label}" if side_label else ""
    if count <= 0:
        return f"- {label}{side_text}: avg N/A | win N/A (0/0)"
    return (
        f"- {label}{side_text}: avg {avg_return:+.1f}% | "
        f"win {_wins_text(win_rate, count)}"
    )


def _momentum_explanation(signal):
    meanings = {
        "LONG_ENTRY": "uptrend entry; MA5 > MA20 > MA60 and all MA slopes are positive.",
        "SHORT_ENTRY": "downtrend entry; MA5 < MA20 < MA60 and all MA slopes are negative.",
        "EXIT_SIGNAL": "no active trend entry; MA alignment or slope strength is not enough.",
        "INSUFFICIENT_DATA": "not enough local price data for MA signal.",
        "NO_SIGNAL": "MA data exists, but no clean entry condition.",
        "ERROR": "signal calculation failed.",
    }
    return meanings.get(signal, "unclassified momentum state.")


def _box_explanation(status):
    meanings = {
        "LONG_RECLAIM": "intraday lower-boundary break recovered by the close; LONG at next open.",
        "SHORT_RECLAIM": "intraday upper-boundary break recovered by the close; SHORT at next open.",
        "BOX_QUALIFIED": "11%~20% box is valid; waiting for a false-break reclaim.",
        "VOLUME_VETO": "box setup exists but turnover surge is too large, so entry is blocked.",
        "NORMAL": "no valid 11%~20% box setup.",
        "DATA_INSUFFICIENT": "not enough local price data for box signal.",
        "ERROR": "box calculation failed.",
    }
    return meanings.get(status, "unclassified box state.")


def _reversion_explanation(signal):
    meanings = {
        "LONG_BIAS": "downside shock has reversed at the close within a non-negative MA60 trend.",
        "SHORT_BIAS": "upside shock has reversed at the close within a non-positive MA60 trend.",
        "DATA_INSUFFICIENT": "not enough local OHLCV data for the 60-day trend filter.",
        "NO_SIGNAL": "no confirmed price/liquidity reversion setup today.",
        "ERROR": "value reversion calculation failed.",
    }
    return meanings.get(signal, "unclassified value reversion state.")


def run_compiled_scan(chat_id, progress_callback=None):
    session = get_or_create_session(chat_id)
    compiled_universe = compile_selected_universe(session)
    if not compiled_universe:
        raise ValueError("No tickers selected. Set a universe first.")

    selected_strategy = session["strategy"]
    strategy_label = STRATEGY_LABELS.get(selected_strategy, selected_strategy)
    custom_tickers = set(session.get("custom_tickers", []))

    missing_before_scan = market_data_store.get_missing_tickers(compiled_universe)
    missing_custom = [ticker for ticker in missing_before_scan if ticker in custom_tickers]
    if missing_custom:
        if progress_callback:
            progress_callback(
                f"Downloading new Direct Input tickers ({len(missing_custom)})..."
            )
        market_data_store.ensure_price_data(missing_custom, min_period="1y")

    if progress_callback:
        progress_callback(
            f"Running {strategy_label} scan on {len(compiled_universe)} tickers using local DB..."
        )

    if selected_strategy == "MA_CROSS":
        report = ma_cross_engine.scan_ma_cross(compiled_universe)
    elif selected_strategy == "BOX_RANGE":
        report = box_range_engine.scan_box_range(compiled_universe)
    elif selected_strategy == "VALUE_REVERSION":
        report = value_reversion_engine.scan_value_reversion(compiled_universe)
    else:
        raise ValueError(f"Unknown strategy: {selected_strategy}")

    if _should_hide_backtest_detail(chat_id) and "Backtest Summary" in report:
        report = report.split("Backtest Summary")[0].strip()
        report += "\n\n*Backtest detail is available in private/admin sessions.*"

    cache_line = market_data_store.freshness_summary(compiled_universe)
    missing = market_data_store.get_missing_tickers(compiled_universe)
    if missing:
        shown = ", ".join(missing[:30])
        suffix = f" ... +{len(missing) - 30} more" if len(missing) > 30 else ""
        cache_line += (
            f"\nMissing local data: {len(missing)} tickers"
            f"\nRun Admin -> Refresh Selected Universe. Missing sample: {shown}{suffix}"
        )
    report = f"{report}\n\n----------------------------------------\n{cache_line}"

    return report


def quick_report(ticker):
    market_data_store.ensure_price_data([ticker], min_period="1y")
    ma_signal, ma_strength = ma_cross_engine.analyze_trend_strict_signals(ticker)
    box = box_range_engine.analyze_box_range(ticker)
    reversion = value_reversion_engine.analyze_value_reversion(ticker)

    box_status = box.get("status", "ERROR")
    box_width = box.get("box_width_pct")
    box_width_text = f"{box_width * 100:.1f}%" if isinstance(box_width, float) else "N/A"
    reversion_status = reversion.get("status", "ERROR")
    reversion_score = reversion.get("score", 0.0)
    if reversion_status == "OK":
        reversion_signal = "LONG_BIAS" if reversion_score >= 0 else "SHORT_BIAS"
    else:
        reversion_signal = reversion_status

    if ma_signal == "LONG_ENTRY" and box_status == "LONG_RECLAIM":
        view = "Strong long watch: MA uptrend plus lower false-break reclaim."
    elif ma_signal == "SHORT_ENTRY" and box_status == "SHORT_RECLAIM":
        view = "Strong short watch: MA downtrend plus upper false-break reclaim."
    elif reversion_signal == "LONG_BIAS" and box_status == "LONG_RECLAIM":
        view = "Reversion long bias with lower false-break reclaim."
    elif reversion_signal == "SHORT_BIAS" and box_status == "SHORT_RECLAIM":
        view = "Reversion short bias with upper false-break reclaim."
    elif box_status in ["LONG_RECLAIM", "SHORT_RECLAIM"]:
        view = "Box reclaim signal is active for next-open entry."
    elif ma_signal in ["LONG_ENTRY", "SHORT_ENTRY"]:
        view = "Trend signal active. Box signal is not the main driver."
    elif reversion_status == "OK":
        view = "Price/liquidity reversion signal is active for next-open entry."
    else:
        view = "No high-priority action signal."

    ma_side = None
    ma_side_label = "LONG+SHORT"
    if ma_signal == "LONG_ENTRY":
        ma_side = "LONG"
        ma_side_label = "LONG"
    elif ma_signal == "SHORT_ENTRY":
        ma_side = "SHORT"
        ma_side_label = "SHORT"

    box_side = None
    box_side_label = "LONG+SHORT"
    if box_status == "LONG_RECLAIM":
        box_side = "LONG"
        box_side_label = "LONG"
    elif box_status == "SHORT_RECLAIM":
        box_side = "SHORT"
        box_side_label = "SHORT"

    ma_bt = ma_cross_engine.run_isolated_backtest(
        ticker,
        side_filter=ma_side,
        period="6mo",
    )
    box_bt = box_range_engine.run_box_backtest(
        ticker,
        side_filter=box_side,
        period="6mo",
    )

    reversion_side = None
    reversion_side_label = ""
    reversion_bt = (0.0, 0.0, 0, "N/A")
    if reversion_status == "OK":
        reversion_side = "LONG" if reversion_score >= 0 else "SHORT"
        reversion_side_label = reversion_side
        reversion_bt = value_reversion_engine.run_reversion_backtest(
            ticker,
            reversion_side,
            period_days=130,
        )

    report = f"**[Quick Check] {ticker}**\n\n"
    report += "**Signals**\n"
    report += f"- Momentum: `{ma_signal}` | strength {ma_strength:+.2f}\n"
    report += f"  Meaning: {_momentum_explanation(ma_signal)}\n"
    report += "  Benchmark: strength above 0 supports LONG, below 0 supports SHORT; stronger magnitude means stronger slope.\n"
    report += f"- Box Range: `{box_status}` | width {box_width_text}\n"
    report += f"  Meaning: {_box_explanation(box_status)}\n"
    report += "  Benchmark: prior 20d box width 11%~20%, 2+ same-side bounces, close reclaim, and turnover below 2x its 20d average.\n"
    report += f"- Price/Liquidity Reversion v2: `{reversion_signal}`"
    if reversion_status == "OK":
        report += f" | score {reversion_score:+.2f}"
    report += "\n"
    report += f"  Meaning: {_reversion_explanation(reversion_signal)}\n"
    report += "  Benchmark: 5d shock within 3d, near-flat/trend-aligned MA60, reversal close, and turnover below 2.0x its 20d average.\n"
    if box.get("message"):
        report += f"\nBox Detail: {box['message']}\n"
    report += f"\nView: {view}\n\n"
    report += "**6M Backtest Summary**\n"
    report += "Entry: strategy signal at close; enter next open.\n"
    report += "Exit: each strategy's configured exit condition.\n"
    report += _bt_line("Momentum", ma_bt[0], ma_bt[1], ma_bt[2], ma_side_label) + "\n"
    report += _bt_line("Box Range", box_bt[0], box_bt[1], box_bt[2], box_side_label) + "\n"
    report += (
        _bt_line(
            "Reversion v2",
            reversion_bt[0],
            reversion_bt[1],
            reversion_bt[2],
            reversion_side_label,
        )
        + "\n"
    )
    return report


def boxbt_report(ticker):
    market_data_store.ensure_price_data([ticker], min_period="1y")
    rows = box_range_engine.compare_box_exit_modes(ticker)
    report = f"**[Box TP/SL Test] {ticker}**\n"
    report += "Period: 1Y | Entry: false-break reclaim at close; enter next open\n\n"
    for row in rows:
        report += (
            f"- {row['label']}: avg {row['avg_return']:+.1f}% | "
            f"win {row['win_rate']:.1f}% ({row['count']} trades)\n"
        )
    return report
