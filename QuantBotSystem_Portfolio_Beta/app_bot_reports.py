# File name: bot_reports.py
# Generates strategy scan, quick-check, and box backtest reports.
from dataclasses import dataclass

import strategy_ma_cross
import strategy_box_range
import core_market_data_store
import strategy_price_liquidity_reversion
import strategy_residual_stat_arb_app
import core_universe_backtest
import app_runtime_lock

from app_bot_session import (
    STRATEGY_LABELS,
    compile_selected_universe,
    get_or_create_session,
    store_backtest_report,
)


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Immutable strategy settings captured when a scan is submitted."""

    universe: tuple[str, ...]
    strategy: str
    neutralization: str
    position_mode: str
    custom_tickers: frozenset[str]


def snapshot_scan_request(chat_id):
    session = get_or_create_session(chat_id)
    return ScanRequest(
        universe=tuple(compile_selected_universe(session)),
        strategy=session["strategy"],
        neutralization=session.get("neutralization", "NONE"),
        position_mode=session.get("position_mode", "LONG_SHORT_NEUTRAL"),
        custom_tickers=frozenset(session.get("custom_tickers", [])),
    )


def _emit_scan_progress(
    progress_callback,
    stage,
    completed,
    total,
    ticker=None,
    succeeded=True,
):
    """Keep presentation callback failures out of strategy calculations."""
    if progress_callback is None:
        return
    try:
        progress_callback(
            stage,
            int(completed),
            int(total),
            ticker,
            bool(succeeded),
        )
    except Exception:
        pass


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
        "LONG_RANGE_RECLAIM": "flat lower-boundary false break recovered by the close; LONG at next open.",
        "SHORT_RANGE_RECLAIM": "flat upper-boundary false break recovered by the close; SHORT at next open.",
        "LONG_TREND_PULLBACK": "rising-channel support rebound confirmed at the close; LONG at next open.",
        "SHORT_TREND_RALLY": "falling-channel resistance rejection confirmed at the close; SHORT at next open.",
        "CHANNEL_QUALIFIED": "a robust channel is valid; waiting for its setup-specific event.",
        "VOLUME_VETO": "channel setup exists but turnover surge is too large, so entry is blocked.",
        "TRANSITION": "the channel is transitioning and is not tradable.",
        "NORMAL": "no valid flat/rising/falling channel setup.",
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


def run_compiled_scan(
    chat_id,
    progress_callback=None,
    return_report_id=False,
    request=None,
):
    with app_runtime_lock.serialized_runtime_operation():
        return _run_compiled_scan(
            chat_id,
            progress_callback=progress_callback,
            return_report_id=return_report_id,
            request=request,
        )


def _run_compiled_scan(
    chat_id,
    progress_callback=None,
    return_report_id=False,
    request=None,
):
    session = get_or_create_session(chat_id)
    request = request or snapshot_scan_request(chat_id)
    compiled_universe = list(request.universe)
    if not compiled_universe:
        raise ValueError("No tickers selected. Set a universe first.")

    selected_strategy = request.strategy
    neutralization = request.neutralization
    position_mode = request.position_mode
    strategy_label = STRATEGY_LABELS.get(selected_strategy, selected_strategy)
    custom_tickers = set(request.custom_tickers)

    missing_before_scan = core_market_data_store.get_missing_tickers(compiled_universe)
    missing_custom = [ticker for ticker in missing_before_scan if ticker in custom_tickers]
    if missing_custom:
        _emit_scan_progress(
            progress_callback,
            "DATA_DOWNLOAD",
            0,
            len(missing_custom),
        )
        core_market_data_store.ensure_price_data(
            missing_custom,
            min_period="1y",
            progress_callback=lambda completed, total, ticker: _emit_scan_progress(
                progress_callback,
                "DATA_DOWNLOAD",
                completed,
                total,
                ticker,
                True,
            ),
        )

    _emit_scan_progress(
        progress_callback,
        "PREPARING",
        1,
        1,
        strategy_label,
        True,
    )
    scan_kwargs = {
        "neutralization": neutralization,
        "position_mode": position_mode,
    }
    if progress_callback is not None:
        scan_kwargs["progress_callback"] = progress_callback

    if selected_strategy == "MA_CROSS":
        report = strategy_ma_cross.scan_ma_cross(
            compiled_universe,
            **scan_kwargs,
        )
    elif selected_strategy == "BOX_RANGE":
        report = strategy_box_range.scan_box_range(
            compiled_universe,
            **scan_kwargs,
        )
    elif selected_strategy == "VALUE_REVERSION":
        report = strategy_price_liquidity_reversion.scan_value_reversion(
            compiled_universe,
            **scan_kwargs,
        )
    elif selected_strategy == "RESIDUAL_STAT_ARB":
        report = strategy_residual_stat_arb_app.scan_residual_stat_arb(
            compiled_universe,
            **scan_kwargs,
        )
    else:
        raise ValueError(f"Unknown strategy: {selected_strategy}")

    report, detail, csv_path = core_universe_backtest.split_report_payload(report)
    report_id = store_backtest_report(session, detail, csv_path)

    cache_line = core_market_data_store.freshness_summary(compiled_universe)
    missing = core_market_data_store.get_missing_tickers(compiled_universe)
    if missing:
        shown = ", ".join(missing[:30])
        suffix = f" ... +{len(missing) - 30} more" if len(missing) > 30 else ""
        cache_line += (
            f"\nMissing local data: {len(missing)} tickers"
            f"\nRun Admin -> Refresh Selected Universe. Missing sample: {shown}{suffix}"
        )
    report = f"{report}\n\n----------------------------------------\n{cache_line}"
    _emit_scan_progress(
        progress_callback,
        "FINALIZING",
        1,
        1,
        None,
        True,
    )

    if return_report_id:
        return report, report_id
    return report


def quick_report(ticker):
    with app_runtime_lock.serialized_runtime_operation():
        return _quick_report(ticker)


def _quick_report(ticker):
    core_market_data_store.ensure_price_data([ticker], min_period="1y")
    ma_signal, ma_strength = strategy_ma_cross.analyze_trend_strict_signals(ticker)
    box = strategy_box_range.analyze_box_range(ticker)
    reversion = strategy_price_liquidity_reversion.analyze_value_reversion(ticker)

    box_status = box.get("status", "ERROR")
    box_width = box.get("box_width_pct")
    box_width_text = f"{box_width * 100:.1f}%" if isinstance(box_width, float) else "N/A"
    reversion_status = reversion.get("status", "ERROR")
    reversion_score = reversion.get("score", 0.0)
    if reversion_status == "OK":
        reversion_signal = "LONG_BIAS" if reversion_score >= 0 else "SHORT_BIAS"
    else:
        reversion_signal = reversion_status

    box_long = box_status in ("LONG_RANGE_RECLAIM", "LONG_TREND_PULLBACK")
    box_short = box_status in ("SHORT_RANGE_RECLAIM", "SHORT_TREND_RALLY")
    if ma_signal == "LONG_ENTRY" and box_long:
        view = "Strong long watch: MA uptrend plus lower false-break reclaim."
    elif ma_signal == "SHORT_ENTRY" and box_short:
        view = "Strong short watch: MA downtrend plus upper false-break reclaim."
    elif reversion_signal == "LONG_BIAS" and box_long:
        view = "Reversion long bias with lower false-break reclaim."
    elif reversion_signal == "SHORT_BIAS" and box_short:
        view = "Reversion short bias with upper false-break reclaim."
    elif box_long or box_short:
        view = "Box/channel signal is active for next-open entry."
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
    if box_long:
        box_side = "LONG"
        box_side_label = "LONG"
    elif box_short:
        box_side = "SHORT"
        box_side_label = "SHORT"

    ma_bt = strategy_ma_cross.run_isolated_backtest(
        ticker,
        side_filter=ma_side,
        period="6mo",
    )
    box_bt = strategy_box_range.run_box_backtest(
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
        reversion_bt = strategy_price_liquidity_reversion.run_reversion_backtest(
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
    report += "  Benchmark: prior 40 completed bars, containment >=85%, width 4~12 ATR, 2+ spaced touches, setup confirmation, and turnover below 2x.\n"
    report += f"- Reversion: `{reversion_signal}`"
    if reversion_status == "OK":
        report += f" | score {reversion_score:+.2f}"
    report += "\n"
    report += f"  Meaning: {_reversion_explanation(reversion_signal)}\n"
    report += "  Benchmark: 5d shock within 3d, near-flat/trend-aligned MA60, reversal close, and turnover below 2.0x its 20d average.\n"
    report += (
        "- Residual Stat-Arb: full-universe scan only; a standalone Quick "
        "Check cannot build its same-country/sector peer hedge.\n"
    )
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
            "Reversion",
            reversion_bt[0],
            reversion_bt[1],
            reversion_bt[2],
            reversion_side_label,
        )
        + "\n"
    )
    return report
