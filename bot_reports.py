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
    box_rows = box_range_engine.compare_box_exit_modes(ticker)

    box_status = box.get("status", "ERROR")
    box_width = box.get("box_width_pct")
    box_width_text = f"{box_width * 100:.1f}%" if isinstance(box_width, float) else "N/A"

    if ma_signal == "LONG_ENTRY" and box_status == "TOUCH_LOWER":
        view = "Strong long watch: MA uptrend plus lower box touch."
    elif ma_signal == "SHORT_ENTRY" and box_status == "TOUCH_UPPER":
        view = "Strong short watch: MA downtrend plus upper box touch."
    elif box_status in ["TOUCH_UPPER", "TOUCH_LOWER"]:
        view = "Box boundary event. Check chart and liquidity before action."
    elif ma_signal in ["LONG_ENTRY", "SHORT_ENTRY"]:
        view = "Trend signal active. Box signal is not the main driver."
    else:
        view = "No high-priority action signal."

    report = f"**[Quick Check] {ticker}**\n\n"
    report += f"MA Signal: `{ma_signal}` ({ma_strength:+.2f})\n"
    report += f"Box Signal: `{box_status}` | width {box_width_text}\n"
    if box.get("message"):
        report += f"Box Detail: {box['message']}\n"
    report += f"\nView: {view}\n\n"
    report += "**Box TP/SL Test**\n"
    for row in box_rows:
        report += (
            f"- {row['label']}: avg {row['avg_return']:+.1f}% | "
            f"win {row['win_rate']:.1f}% ({row['count']} trades)\n"
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
