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
        "LONG_ENTRY": "uptrend entry; MA5 > MA10 > MA20 and all MA slopes are above +1%.",
        "SHORT_ENTRY": "downtrend entry; MA5 < MA10 < MA20 and all MA slopes are below -1%.",
        "EXIT_SIGNAL": "no active trend entry; MA alignment or slope strength is not enough.",
        "INSUFFICIENT_DATA": "not enough local price data for MA signal.",
        "NO_SIGNAL": "MA data exists, but no clean entry condition.",
        "ERROR": "signal calculation failed.",
    }
    return meanings.get(signal, "unclassified momentum state.")


def _box_explanation(status):
    meanings = {
        "TOUCH_LOWER": "price is near the lower edge of a valid box; rebound watch.",
        "TOUCH_UPPER": "price is near the upper edge of a valid box; breakout or trim-risk watch.",
        "BOX_FORMED": "range is narrow enough, but price is not near either edge.",
        "NORMAL": "no active box setup; recent range is too wide or not near an edge.",
        "DATA_INSUFFICIENT": "not enough local price data for box signal.",
        "ERROR": "box calculation failed.",
    }
    return meanings.get(status, "unclassified box state.")


def _reversion_explanation(signal):
    meanings = {
        "LONG_BIAS": "price/value proxy is below its short-term norm; rebound bias.",
        "SHORT_BIAS": "price/value proxy is above its short-term norm; reversal-risk bias.",
        "FUNDAMENTAL_MISSING": "fundamental data is missing, so value signal is unavailable.",
        "DATA_INSUFFICIENT": "not enough local price data for value signal.",
        "DATA_SUSPECT": "fundamental ratio looks abnormal, so signal is ignored.",
        "NO_SIGNAL": "value data exists, but z-score is not usable.",
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
    report = f"{cache_line}\n\n{report}"

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

    if ma_signal == "LONG_ENTRY" and box_status == "TOUCH_LOWER":
        view = "Strong long watch: MA uptrend plus lower box touch."
    elif ma_signal == "SHORT_ENTRY" and box_status == "TOUCH_UPPER":
        view = "Strong short watch: MA downtrend plus upper box touch."
    elif reversion_signal == "LONG_BIAS" and box_status == "TOUCH_LOWER":
        view = "Reversion long bias with lower box support. Check chart timing."
    elif reversion_signal == "SHORT_BIAS" and box_status == "TOUCH_UPPER":
        view = "Reversion short bias near upper box risk area."
    elif box_status in ["TOUCH_UPPER", "TOUCH_LOWER"]:
        view = "Box boundary event. Check chart and liquidity before action."
    elif ma_signal in ["LONG_ENTRY", "SHORT_ENTRY"]:
        view = "Trend signal active. Box signal is not the main driver."
    elif reversion_status == "OK":
        view = "Value reversion signal is available, but no strong MA/box confirmation."
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
    if box_status == "TOUCH_LOWER":
        box_side = "LONG"
        box_side_label = "LONG"
    elif box_status == "TOUCH_UPPER":
        box_side = "SHORT"
        box_side_label = "SHORT"

    ma_bt = ma_cross_engine.run_isolated_backtest(
        ticker,
        threshold_pct=1.0,
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
    report += "  Benchmark: strength above +1 supports LONG, below -1 supports SHORT.\n"
    report += f"- Box Range: `{box_status}` | width {box_width_text}\n"
    report += f"  Meaning: {_box_explanation(box_status)}\n"
    report += "  Benchmark: valid box width is 20% or less; edge touch buffer is 1%.\n"
    report += f"- Value Reversion: `{reversion_signal}`"
    if reversion_status == "OK":
        report += f" | score {reversion_score:+.2f}"
    report += "\n"
    report += f"  Meaning: {_reversion_explanation(reversion_signal)}\n"
    report += "  Benchmark: score magnitude near 1 is meaningful, near 2 is strong.\n"
    if box.get("message"):
        report += f"\nBox Detail: {box['message']}\n"
    report += f"\nView: {view}\n\n"
    report += "**6M Backtest Summary**\n"
    report += "Rule: signal close -> next open, no-lookahead historical entries.\n"
    report += _bt_line("Momentum", ma_bt[0], ma_bt[1], ma_bt[2], ma_side_label) + "\n"
    report += _bt_line("Box Range", box_bt[0], box_bt[1], box_bt[2], box_side_label) + "\n"
    report += (
        _bt_line(
            "Value Reversion",
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
    report += "Period: 1Y | Entry: Donchian box upper/lower touch\n\n"
    for row in rows:
        report += (
            f"- {row['label']}: avg {row['avg_return']:+.1f}% | "
            f"win {row['win_rate']:.1f}% ({row['count']} trades)\n"
        )
    return report
