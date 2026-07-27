# File name: ma_cross_engine.py
# Generates moving-average momentum signals and backtest reports.
import numpy as np
import pandas as pd
import core_market_data_store
import core_universe_backtest
import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_position_mode
import core_signal_scoring
from core_signal_model import Signal


MOMENTUM_MA_WINDOWS = (5, 20, 60)
MOMENTUM_SLOPE_THRESHOLD = 0.0
MOMENTUM_HOLDING_DAYS = 20
TURNOVER_LOOKBACK_DAYS = 252
TURNOVER_MULTIPLE = 3.0


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Open", "Close", "Volume"]).copy()


def _period_to_rows(period):
    if period == "1mo":
        return 35
    if period == "3mo":
        return 90
    if period == "4mo":
        return 120
    if period == "6mo":
        return 130
    if period == "1y":
        return 260
    if period == "14mo":
        return 330
    if period == "2y":
        return 540
    if period == "3y":
        return 800
    if period == "4y":
        return 1060
    return None


def _load_ohlcv(ticker, period="14mo", allow_download=False):
    df = core_market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
    if not df.empty:
        return _normalize_ohlcv(df)

    if allow_download:
        core_market_data_store.ensure_price_data([ticker], min_period="2y")
        df = core_market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
        if not df.empty:
            return _normalize_ohlcv(df)

    return pd.DataFrame()


def _add_ma_signal_columns(df, windows=MOMENTUM_MA_WINDOWS, threshold_pct=MOMENTUM_SLOPE_THRESHOLD):
    fast, mid, slow = windows
    out = df.copy()
    out["MA_FAST"] = out["Close"].rolling(window=fast).mean()
    out["MA_MID"] = out["Close"].rolling(window=mid).mean()
    out["MA_SLOW"] = out["Close"].rolling(window=slow).mean()

    out["S_FAST"] = out["MA_FAST"].pct_change() * 100
    out["S_MID"] = out["MA_MID"].pct_change() * 100
    out["S_SLOW"] = out["MA_SLOW"].pct_change() * 100
    out["TURNOVER_PROXY"] = out["Volume"].astype(float) * out["Close"].astype(float)
    out["TURNOVER_MEAN"] = out["TURNOVER_PROXY"].rolling(
        TURNOVER_LOOKBACK_DAYS,
        min_periods=TURNOVER_LOOKBACK_DAYS,
    ).mean()
    out["TURNOVER_SURGE"] = (
        (out["TURNOVER_MEAN"] > 0)
        & (out["TURNOVER_PROXY"] > TURNOVER_MULTIPLE * out["TURNOVER_MEAN"])
    )

    long_ma = (
        (out["MA_FAST"] > out["MA_MID"])
        & (out["MA_MID"] > out["MA_SLOW"])
        & (out["S_FAST"] > threshold_pct)
        & (out["S_MID"] > threshold_pct)
        & (out["S_SLOW"] > threshold_pct)
    )
    short_ma = (
        (out["MA_FAST"] < out["MA_MID"])
        & (out["MA_MID"] < out["MA_SLOW"])
        & (out["S_FAST"] < -threshold_pct)
        & (out["S_MID"] < -threshold_pct)
        & (out["S_SLOW"] < -threshold_pct)
    )
    out["LONG_SIGNAL"] = long_ma & out["TURNOVER_SURGE"]
    out["SHORT_SIGNAL"] = short_ma & out["TURNOVER_SURGE"]
    out["MOMENTUM_ALIGNED"] = long_ma | short_ma
    out["SIGNAL_STRENGTH"] = (out["S_FAST"] + out["S_MID"] + out["S_SLOW"]) / 3.0
    return out


def _position_state(df, side_filter=None):
    position = None
    entry_idx = None
    for idx in range(len(df) - 1):
        if position is None:
            if df["LONG_SIGNAL"].iloc[idx] and side_filter in (None, "LONG"):
                position = "LONG"
                entry_idx = idx + 1
            elif df["SHORT_SIGNAL"].iloc[idx] and side_filter in (None, "SHORT"):
                position = "SHORT"
                entry_idx = idx + 1
        elif idx - entry_idx >= MOMENTUM_HOLDING_DAYS:
            position = None
            entry_idx = None
    return position, entry_idx


def analyze_trend_strict_signals(
    ticker,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    period="14mo",
    windows=MOMENTUM_MA_WINDOWS,
):
    """Calculate MA alignment and slope signal for one ticker."""
    try:
        df = _load_ohlcv(ticker, period=period)

        if df.empty or len(df) < max(max(windows), TURNOVER_LOOKBACK_DAYS) + 1:
            return "INSUFFICIENT_DATA", 0.0

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)
        latest = df.iloc[-1]
        required = [
            "MA_FAST", "MA_MID", "MA_SLOW", "S_FAST", "S_MID", "S_SLOW",
            "TURNOVER_MEAN",
        ]
        if pd.isna([latest[name] for name in required]).any():
            return "NO_SIGNAL", 0.0

        signal_strength = float(latest["SIGNAL_STRENGTH"])

        if bool(latest["LONG_SIGNAL"]):
            return "LONG_ENTRY", signal_strength
        if bool(latest["SHORT_SIGNAL"]):
            return "SHORT_ENTRY", signal_strength
        if bool(latest["MOMENTUM_ALIGNED"]):
            return "WAIT_VOLUME", signal_strength
        return "EXIT_SIGNAL", signal_strength
    except Exception:
        return "ERROR", 0.0


def calculate_portfolio_weights(top_longs, top_shorts):
    """Allocate 50% gross long and 50% gross short by rank-weighted top names."""
    weights = {}

    def get_ticker(item):
        if isinstance(item, dict):
            return item["ticker"]
        return item[0]

    if top_longs:
        long_ranks = np.arange(len(top_longs), 0, -1)
        total_rank = np.sum(long_ranks)
        for idx, item in enumerate(top_longs):
            ticker = get_ticker(item)
            weights[ticker] = (long_ranks[idx] / total_rank) * 50.0

    if top_shorts:
        short_ranks = np.arange(len(top_shorts), 0, -1)
        total_rank = np.sum(short_ranks)
        for idx, item in enumerate(top_shorts):
            ticker = get_ticker(item)
            weights[ticker] = (short_ranks[idx] / total_rank) * 50.0

    return weights


def run_isolated_backtest(
    ticker,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    side_filter=None,
    period="2y",
    windows=MOMENTUM_MA_WINDOWS,
    holding_days=MOMENTUM_HOLDING_DAYS,
    include_trades=False,
    evaluation_days=None,
    evaluation_start=None,
    evaluation_end=None,
    df=None,
    entry_execution=core_entry_execution.NEXT_OPEN,
    adverse_gap_limit_pct=core_entry_execution.DEFAULT_ADVERSE_GAP_LIMIT_PCT,
):
    """Run a simple isolated MA momentum backtest."""
    try:
        df = _load_ohlcv(ticker, period=period) if df is None else _normalize_ohlcv(df)
        if evaluation_end:
            df = df.loc[:pd.Timestamp(evaluation_end)]
        if df.empty or len(df) < TURNOVER_LOOKBACK_DAYS + holding_days + 1:
            result = (0.0, 0.0, 0, "N/A", [])
            return result if include_trades else result[:4]

        warmup_idx = max(max(windows), TURNOVER_LOOKBACK_DAYS)
        signal_start_idx = (
            max(warmup_idx, len(df) - evaluation_days)
            if evaluation_days
            else warmup_idx
        )
        requested_start = pd.Timestamp(evaluation_start) if evaluation_start else df.index[signal_start_idx + 1]
        start_date = max(df.index[min(signal_start_idx + 1, len(df) - 1)], requested_start).date().isoformat()
        end_date = df.index[-1].date().isoformat()
        period_label = f"{start_date} to {end_date}"

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)

        trades = []
        trade_details = []

        next_available_idx = 0
        for idx in range(signal_start_idx, len(df) - 1):
            if idx + 1 < next_available_idx:
                continue
            entry_idx = idx + 1
            planned_exit_idx = idx + holding_days
            exit_idx = min(planned_exit_idx, len(df) - 1)
            if evaluation_start and df.index[entry_idx] < pd.Timestamp(evaluation_start):
                continue
            signal_side = (
                "LONG"
                if df["LONG_SIGNAL"].iloc[idx] and side_filter in (None, "LONG")
                else "SHORT"
                if df["SHORT_SIGNAL"].iloc[idx] and side_filter in (None, "SHORT")
                else None
            )
            if signal_side is None:
                continue
            execution = core_entry_execution.resolve_entry(
                df,
                idx,
                signal_side,
                mode=entry_execution,
                adverse_gap_limit_pct=adverse_gap_limit_pct,
            )
            if execution is None:
                continue
            entry_price = execution["price"]
            exit_price = float(df["Close"].iloc[exit_idx])
            execution_fields = {
                "entry_execution": execution["mode"],
                "signal_close": execution["signal_close"],
                "next_open": execution["next_open"],
                "signal_to_open_return_pct": execution["signal_to_open_return_pct"],
                "signal_to_fill_return_pct": execution["signal_to_fill_return_pct"],
            }
            exit_reason = (
                f"{holding_days}d holding exit"
                if planned_exit_idx <= len(df) - 1
                else "evaluation end mark"
            )
            exit_event = {
                "date": df.index[exit_idx].date().isoformat(),
                "reason": exit_reason,
                "weight": 1.0,
                "price": exit_price,
            }
            if signal_side == "LONG":
                trade_return = (exit_price / entry_price - 1.0) * 100.0
                trades.append(trade_return)
                trade_details.append(
                    {
                        "side": "LONG",
                        "entry_date": df.index[entry_idx].date().isoformat(),
                        "entry_price": entry_price,
                        "exit_date": df.index[exit_idx].date().isoformat(),
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "exit_events": [exit_event],
                        "signal_score": float(df["SIGNAL_STRENGTH"].iloc[idx]),
                        "return": trade_return,
                        **execution_fields,
                    }
                )
                next_available_idx = exit_idx + 1
            else:
                trade_return = (entry_price - exit_price) / entry_price * 100.0
                trades.append(trade_return)
                trade_details.append(
                    {
                        "side": "SHORT",
                        "entry_date": df.index[entry_idx].date().isoformat(),
                        "entry_price": entry_price,
                        "exit_date": df.index[exit_idx].date().isoformat(),
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "exit_events": [exit_event],
                        "signal_score": float(df["SIGNAL_STRENGTH"].iloc[idx]),
                        "return": trade_return,
                        **execution_fields,
                    }
                )
                next_available_idx = exit_idx + 1

        if not trades:
            result = (0.0, 0.0, 0, period_label, [])
            return result if include_trades else result[:4]

        avg_return = sum(trades) / len(trades)
        win_rate = sum(1 for pnl in trades if pnl > 0) / len(trades) * 100.0
        result = (avg_return, win_rate, len(trades), period_label, trade_details)
        return result if include_trades else result[:4]
    except Exception:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]


def check_new_signal_status(
    ticker,
    signal_type,
    threshold_pct=MOMENTUM_SLOPE_THRESHOLD,
    windows=MOMENTUM_MA_WINDOWS,
):
    """Detect whether today's signal is the first day of a new entry signal."""
    try:
        df = _load_ohlcv(ticker, period="14mo")
        if df.empty or len(df) < max(max(windows), TURNOVER_LOOKBACK_DAYS) + 2:
            return False

        df = _add_ma_signal_columns(df, windows=windows, threshold_pct=threshold_pct)

        if signal_type == "LONG":
            return bool(df["LONG_SIGNAL"].iloc[-1] and not df["LONG_SIGNAL"].iloc[-2])
        return bool(df["SHORT_SIGNAL"].iloc[-1] and not df["SHORT_SIGNAL"].iloc[-2])
    except Exception:
        return False


def _format_ranked_rows(items, long_side=True):
    if not items:
        return "None\n"

    rows = []
    for idx, (
        ticker,
        strength,
        neutralized_score,
        rank_score,
        requested_weight,
    ) in enumerate(items, start=1):
        sign = "+" if long_side else ""
        rows.append(
            f"{idx}. {ticker:<6} | raw {sign}{strength:.2f}% | "
            f"neutral {neutralized_score:+.2f} | rank {rank_score:+.2f} | "
            f"alpha weight {requested_weight * 100:.1f}%"
        )
    return "\n".join(rows) + "\n"


def _notify_progress(
    progress_callback,
    stage,
    completed,
    total,
    ticker,
    succeeded,
):
    if progress_callback is None:
        return
    try:
        progress_callback(stage, completed, total, ticker, succeeded)
    except Exception:
        pass


def _stage_progress_callback(progress_callback, stage):
    if progress_callback is None:
        return None

    def notify(completed, total, ticker, succeeded):
        _notify_progress(
            progress_callback,
            stage,
            completed,
            total,
            ticker,
            succeeded,
        )

    return notify


def scan_ma_cross(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    progress_callback=None,
):
    long_list, short_list, exit_list = [], [], []
    new_longs, new_shorts = [], []
    failed_tickers = []
    raw_scores = {}

    total_tickers = len(universe_list)
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            signal, strength = analyze_trend_strict_signals(ticker)
            succeeded = signal not in ("INSUFFICIENT_DATA", "ERROR")

            if signal in ["INSUFFICIENT_DATA", "ERROR", "NO_SIGNAL"]:
                failed_tickers.append(ticker)
                continue

            raw_scores[ticker] = strength

            if signal == "LONG_ENTRY":
                long_list.append((ticker, strength))
                if check_new_signal_status(ticker, "LONG"):
                    new_longs.append(ticker)
            elif signal == "SHORT_ENTRY":
                short_list.append((ticker, strength))
                if check_new_signal_status(ticker, "SHORT"):
                    new_shorts.append(ticker)
            elif signal == "EXIT_SIGNAL":
                exit_list.append((ticker, strength))
        except Exception:
            failed_tickers.append(ticker)
        finally:
            _notify_progress(
                progress_callback,
                "CURRENT_SCAN",
                completed,
                total_tickers,
                ticker,
                succeeded,
            )

    neutralized_scores = core_cross_sectional_neutralization.neutralize_scores(
        raw_scores, neutralization
    )
    neutralization = core_cross_sectional_neutralization.normalize_mode(neutralization)
    position_mode = core_position_mode.normalize_mode(position_mode)
    long_list = [
        (ticker, strength, neutralized_scores.get(ticker, strength))
        for ticker, strength in long_list
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) > 0
    ]
    short_list = [
        (ticker, strength, neutralized_scores.get(ticker, strength))
        for ticker, strength in short_list
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) < 0
    ]
    new_longs = [
        ticker for ticker in new_longs
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) > 0
    ]
    new_shorts = [
        ticker for ticker in new_shorts
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) < 0
    ]
    if not core_position_mode.allows_long(position_mode):
        long_list = []
        new_longs = []
    if not core_position_mode.allows_short(position_mode):
        short_list = []
        new_shorts = []
    candidate_pairs = []
    for side, rows in (("LONG", long_list), ("SHORT", short_list)):
        for ticker, strength, neutralized_score in rows:
            candidate_pairs.append(
                (
                    Signal(
                        strategy="MA_CROSS",
                        ticker=ticker,
                        side=side,
                        signal_date="LATEST_CLOSE",
                        raw_score=strength,
                        status=f"{side}_ENTRY",
                        entry_execution="NEXT_OPEN",
                    ),
                    neutralized_score,
                )
            )
    scored_by_ticker = {
        item.signal.ticker: item
        for item in core_signal_scoring.score_signal_cohort(candidate_pairs)
    }
    long_list = [
        (
            ticker,
            strength,
            neutralized_score,
            scored_by_ticker[ticker].rank_score,
            scored_by_ticker[ticker].requested_weight,
        )
        for ticker, strength, neutralized_score in long_list
        if ticker in scored_by_ticker
    ]
    short_list = [
        (
            ticker,
            strength,
            neutralized_score,
            scored_by_ticker[ticker].rank_score,
            scored_by_ticker[ticker].requested_weight,
        )
        for ticker, strength, neutralized_score in short_list
        if ticker in scored_by_ticker
    ]
    long_list.sort(key=lambda item: (item[3], item[1]), reverse=True)
    short_list.sort(key=lambda item: (item[3], item[1]))
    exit_list.sort(key=lambda item: item[0])

    top_longs = long_list[:5]
    top_shorts = short_list[:5]

    long_rows = _format_ranked_rows(top_longs, long_side=True)
    short_rows = _format_ranked_rows(top_shorts, long_side=False)

    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_isolated_backtest,
        period=core_backtest_periods.HISTORY_PERIOD,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        progress_callback=_stage_progress_callback(
            progress_callback, "IS_BACKTEST"
        ),
        side_filter=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.selection_window(),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_isolated_backtest,
        period=core_backtest_periods.HISTORY_PERIOD,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        progress_callback=_stage_progress_callback(
            progress_callback, "OOS_BACKTEST"
        ),
        side_filter=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.oos_window(),
    )
    backtest_report = core_universe_backtest.format_fixed_is_oos_backtest(
        selection_result,
        oos_result,
        entry_text=(
            "MA 5/20/60 alignment and same-direction slopes plus "
            f"turnover >{TURNOVER_MULTIPLE:.0f}x its {TURNOVER_LOOKBACK_DAYS}d average "
            "at close; enter next open"
        ),
        exit_text=(
            f"close after {MOMENTUM_HOLDING_DAYS} trading days; no overlapping entries"
        ),
        strategy_key="ma_momentum",
    )

    msg = (
        "**Momentum Scan Report**\n"
        "=============================\n\n"
        "Rule: MA 5/20/60 alignment + same-direction slopes + "
        f"turnover > {TURNOVER_MULTIPLE:.0f}x its {TURNOVER_LOOKBACK_DAYS}d average.\n\n"
        f"{core_cross_sectional_neutralization.report_line(neutralization, raw_scores)}\n"
        f"{core_position_mode.report_line(position_mode)}\n"
        "Scope: today's candidates and entry-date IS/OOS cross-sections; current metadata labels.\n\n"
        "**New Signals Today**\n"
        f"- New LONG: {', '.join(new_longs) if new_longs else 'None'}\n"
        f"- New SHORT: {', '.join(new_shorts) if new_shorts else 'None'}\n\n"
        "-----------------------------\n"
        "**LONG Candidates**\n"
        "Top 5 by momentum score:\n"
        f"{long_rows}\n"
        "-----------------------------\n"
        "**SHORT Candidates**\n"
        "Top 5 by momentum score:\n"
        f"{short_rows}\n"
        f"{backtest_report}"
        "============================="
    )

    if failed_tickers:
        failed_str = ", ".join(sorted(set(failed_tickers)))
        msg += (
            "\n\n----------------------------------------\n"
            "**Data Failed / Insufficient**\n"
            f"`{failed_str}`"
        )

    return msg
