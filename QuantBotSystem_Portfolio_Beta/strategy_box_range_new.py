"""Causal squeeze-breakout box strategy for Portfolio Beta."""

import numpy as np
import pandas as pd

import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_market_data_store
import core_position_mode
import core_signal_scoring
from core_signal_model import Signal
import core_universe_backtest


STRATEGY_KEY = "BOX_RANGE_NEW"
BOX_BARS = 15
ATR_BARS = 20
TURNOVER_BARS = 20
MAX_WIDTH_ATR = 3.0
MIN_TURNOVER_RATIO = 2.0
MAX_ENTRY_EXTENSION = 0.5
TIME_EXIT_SESSIONS = 10
WARMUP_BARS = max(BOX_BARS, ATR_BARS + 1, TURNOVER_BARS)


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in out.columns for column in required):
        return pd.DataFrame()
    return out.dropna(subset=required)


def _period_to_rows(period):
    return {
        "1mo": 35,
        "3mo": 90,
        "6mo": 130,
        "1y": 260,
        "2y": 540,
        "3y": 800,
        "4y": 1060,
    }.get(period)


def _load_ohlcv(ticker, period="1y", allow_download=False):
    rows = _period_to_rows(period)
    df = core_market_data_store.get_price_data(ticker, period_days=rows)
    if not df.empty:
        return _normalize_ohlcv(df)
    if allow_download:
        core_market_data_store.ensure_price_data([ticker], min_period="1y")
        return _normalize_ohlcv(
            core_market_data_store.get_price_data(ticker, period_days=rows)
        )
    return pd.DataFrame()


def _atr_before(df, signal_index):
    """Return ATR20 ending at t-1; one extra bar supplies the first prev-close."""
    history = df.iloc[signal_index - ATR_BARS - 1 : signal_index]
    high = history["High"].to_numpy(dtype=float)
    low = history["Low"].to_numpy(dtype=float)
    close = history["Close"].to_numpy(dtype=float)
    true_range = np.maximum.reduce(
        [
            high[1:] - low[1:],
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ]
    )
    value = float(np.mean(true_range[-ATR_BARS:]))
    return value if np.isfinite(value) and value > 0 else float("nan")


def _signal_stats(df, signal_index):
    if signal_index < WARMUP_BARS or signal_index >= len(df):
        return {"status": "DATA_INSUFFICIENT"}

    box = df.iloc[signal_index - BOX_BARS : signal_index]
    turnover_history = df.iloc[
        signal_index - TURNOVER_BARS : signal_index
    ]
    current = df.iloc[signal_index]
    upper = float(box["High"].max())
    lower = float(box["Low"].min())
    height = upper - lower
    atr = _atr_before(df, signal_index)
    turnover_mean = float(
        (turnover_history["Close"] * turnover_history["Volume"]).mean()
    )
    current_turnover = float(current["Close"] * current["Volume"])
    turnover_ratio = (
        current_turnover / turnover_mean if turnover_mean > 0 else float("nan")
    )
    if height <= 0 or not np.isfinite(atr) or not np.isfinite(turnover_ratio):
        return {"status": "BOX_INVALID"}

    close = float(current["Close"])
    width_atr = height / atr
    midpoint = (upper + lower) / 2.0
    stats = {
        "upper": upper,
        "lower": lower,
        "box_height": height,
        "box_width_pct": height / midpoint if midpoint > 0 else 0.0,
        "atr": atr,
        "width_atr": width_atr,
        "turnover_ratio": turnover_ratio,
        "squeeze_depth": float(np.clip(1.0 - width_atr / MAX_WIDTH_ATR, 0.0, 1.0)),
        "setup": "SQUEEZE_BREAKOUT",
        "structure": "COMPRESSED_BOX",
    }
    if width_atr > MAX_WIDTH_ATR:
        return {
            "status": "BOX_TOO_WIDE",
            "message": f"box width {width_atr:.2f} ATR exceeds {MAX_WIDTH_ATR:.1f}",
            **stats,
        }
    if turnover_ratio < MIN_TURNOVER_RATIO:
        return {
            "status": "TURNOVER_TOO_LOW",
            "message": f"turnover {turnover_ratio:.2f}x is below {MIN_TURNOVER_RATIO:.1f}x",
            **stats,
        }

    side = "LONG" if close > upper else "SHORT" if close < lower else None
    if side is None:
        return {
            "status": "WAIT_BREAKOUT",
            "message": f"waiting outside {lower:.2f}~{upper:.2f}",
            **stats,
        }
    boundary = upper if side == "LONG" else lower
    direction = 1.0 if side == "LONG" else -1.0
    acceptance = float(np.clip(abs(close - boundary) / height, 0.0, 1.0))
    raw_score = direction * (stats["squeeze_depth"] + acceptance) / 2.0
    target = upper + height if side == "LONG" else lower - height
    status = f"{side}_BREAKOUT"
    return {
        "status": status,
        "side": side,
        "acceptance": acceptance,
        "raw_score": raw_score,
        "score": raw_score,
        "target": target,
        "message": (
            f"{side.lower()} breakout; box {lower:.2f}~{upper:.2f}, "
            f"width {width_atr:.2f} ATR, turnover {turnover_ratio:.2f}x"
        ),
        **stats,
    }


def _precompute_candidates(df):
    candidates = [None] * len(df)
    for index in range(WARMUP_BARS, len(df)):
        candidates[index] = _signal_stats(df, index)
    return candidates


def _candidate_prefix(candidates, row_count):
    if candidates is None or len(candidates) < row_count:
        return None
    return candidates[:row_count]


def analyze_box_range_new(ticker, df=None):
    """Analyze the latest completed bar without using it in the box or ATR."""
    try:
        prices = _load_ohlcv(ticker, period="6mo") if df is None else _normalize_ohlcv(df)
        if len(prices) <= WARMUP_BARS:
            return {"status": "DATA_INSUFFICIENT"}
        return _signal_stats(prices, len(prices) - 1)
    except Exception as exc:
        return {"status": "ERROR", "message": str(exc)}


def _valid_next_open(next_open, stats, side):
    extension = MAX_ENTRY_EXTENSION * stats["box_height"]
    if side == "LONG":
        return stats["upper"] < next_open <= stats["upper"] + extension
    return stats["lower"] - extension <= next_open < stats["lower"]


def _pnl_pct(side, exit_price, entry_price):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (exit_price / entry_price - 1.0) * 100.0


def _target_hit(side, day, target):
    return (
        float(day["High"]) >= target
        if side == "LONG"
        else float(day["Low"]) <= target
    )


def _failure_close(side, day, stats):
    return (
        float(day["Close"]) <= stats["upper"]
        if side == "LONG"
        else float(day["Close"]) >= stats["lower"]
    )


def run_box_backtest(
    ticker,
    side_filter=None,
    df=None,
    period="1y",
    include_trades=False,
    evaluation_days=None,
    evaluation_start=None,
    evaluation_end=None,
    entry_execution=core_entry_execution.NEXT_OPEN,
    adverse_gap_limit_pct=core_entry_execution.DEFAULT_ADVERSE_GAP_LIMIT_PCT,
    precomputed_candidates=None,
    fixed_exit_sessions=None,
):
    """Backtest frozen boxes with target, next-open failure, and 10-session exit."""
    try:
        fixed_exit_sessions = core_entry_execution.normalize_fixed_exit_sessions(
            fixed_exit_sessions
        )
        prices = _load_ohlcv(ticker, period=period) if df is None else _normalize_ohlcv(df)
        if evaluation_end:
            prices = prices.loc[: pd.Timestamp(evaluation_end)]
        if len(prices) < WARMUP_BARS + 2:
            result = (0.0, 0.0, 0, "N/A", [])
            return result if include_trades else result[:4]

        signal_start = (
            max(WARMUP_BARS, len(prices) - int(evaluation_days))
            if evaluation_days
            else WARMUP_BARS
        )
        requested_start = (
            pd.Timestamp(evaluation_start)
            if evaluation_start
            else prices.index[min(signal_start + 1, len(prices) - 1)]
        )
        period_start = max(
            prices.index[min(signal_start + 1, len(prices) - 1)], requested_start
        )
        period_label = f"{period_start.date()} to {prices.index[-1].date()}"
        candidates = _candidate_prefix(precomputed_candidates, len(prices))
        if candidates is None:
            candidates = _precompute_candidates(prices)

        returns = []
        details = []
        next_signal_index = signal_start

        def record_trade(side, stats, signal_idx, execution, exit_idx, exit_price, reason):
            trade_return = _pnl_pct(side, exit_price, execution["price"])
            risk = (
                execution["price"] - stats["upper"]
                if side == "LONG"
                else stats["lower"] - execution["price"]
            )
            reward = (
                stats["target"] - execution["price"]
                if side == "LONG"
                else execution["price"] - stats["target"]
            )
            event = {
                "date": prices.index[exit_idx].date().isoformat(),
                "reason": reason,
                "weight": 1.0,
                "price": exit_price,
            }
            returns.append(trade_return)
            details.append(
                {
                    "strategy": STRATEGY_KEY,
                    "side": side,
                    "status": stats["status"],
                    "setup": stats["setup"],
                    "structure": stats["structure"],
                    "signal_date": prices.index[signal_idx].date().isoformat(),
                    "entry_date": prices.index[execution["entry_idx"]].date().isoformat(),
                    "entry_price": execution["price"],
                    "exit_date": prices.index[exit_idx].date().isoformat(),
                    "exit_price": exit_price,
                    "exit_reason": reason,
                    "exit_events": [event],
                    "signal_score": stats["raw_score"],
                    "expected_rr": reward / risk if risk > 0 else 0.0,
                    "upper": stats["upper"],
                    "lower": stats["lower"],
                    "box_height": stats["box_height"],
                    "width_atr": stats["width_atr"],
                    "turnover_ratio": stats["turnover_ratio"],
                    "entry_execution": execution["mode"],
                    "signal_close": execution["signal_close"],
                    "next_open": execution["next_open"],
                    "signal_to_open_return_pct": execution[
                        "signal_to_open_return_pct"
                    ],
                    "signal_to_fill_return_pct": execution[
                        "signal_to_fill_return_pct"
                    ],
                    "return": trade_return,
                }
            )

        for signal_idx in range(signal_start, len(prices) - 1):
            if signal_idx < next_signal_index:
                continue
            stats = candidates[signal_idx]
            if not stats or stats.get("status") not in (
                "LONG_BREAKOUT",
                "SHORT_BREAKOUT",
            ):
                continue
            side = stats["side"]
            if side_filter not in (None, side):
                continue
            entry_idx = signal_idx + 1
            if evaluation_start and prices.index[entry_idx] < pd.Timestamp(evaluation_start):
                continue
            execution = core_entry_execution.resolve_entry(
                prices,
                signal_idx,
                side,
                mode=entry_execution,
                adverse_gap_limit_pct=adverse_gap_limit_pct,
            )
            if execution is None or not _valid_next_open(
                execution["next_open"], stats, side
            ):
                continue

            if fixed_exit_sessions is not None:
                exit_idx = entry_idx + fixed_exit_sessions
                if exit_idx >= len(prices):
                    continue
                record_trade(
                    side,
                    stats,
                    signal_idx,
                    execution,
                    exit_idx,
                    float(prices["Close"].iloc[exit_idx]),
                    f"D+{fixed_exit_sessions} fixed close",
                )
                next_signal_index = exit_idx + 1
                continue

            exit_start = entry_idx if execution["allow_entry_day_exit"] else entry_idx + 1
            pending_failure = False
            exited = False
            for index in range(entry_idx, len(prices)):
                day = prices.iloc[index]
                if pending_failure:
                    record_trade(
                        side,
                        stats,
                        signal_idx,
                        execution,
                        index,
                        float(day["Open"]),
                        "box failure next open",
                    )
                    exited = True
                    break
                if index >= exit_start and _target_hit(side, day, stats["target"]):
                    record_trade(
                        side,
                        stats,
                        signal_idx,
                        execution,
                        index,
                        stats["target"],
                        "box target",
                    )
                    exited = True
                    break
                if index - entry_idx + 1 >= TIME_EXIT_SESSIONS:
                    record_trade(
                        side,
                        stats,
                        signal_idx,
                        execution,
                        index,
                        float(day["Close"]),
                        f"{TIME_EXIT_SESSIONS}-session close",
                    )
                    exited = True
                    break
                pending_failure = _failure_close(side, day, stats)

            exit_idx = index
            if not exited:
                exit_idx = len(prices) - 1
                record_trade(
                    side,
                    stats,
                    signal_idx,
                    execution,
                    exit_idx,
                    float(prices["Close"].iloc[exit_idx]),
                    "evaluation end mark",
                )
            next_signal_index = exit_idx + 1

        if not returns:
            result = (0.0, 0.0, 0, period_label, [])
            return result if include_trades else result[:4]
        result = (
            sum(returns) / len(returns),
            sum(value > 0 for value in returns) / len(returns) * 100.0,
            len(returns),
            period_label,
            details,
        )
        return result if include_trades else result[:4]
    except Exception:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]


def _notify_progress(progress_callback, stage, completed, total, ticker, succeeded):
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
        _notify_progress(progress_callback, stage, completed, total, ticker, succeeded)

    return notify


def _candidate_row(index, ticker, result, neutralized_score, rank_score, weight):
    side = result["side"]
    gate = (
        f"{result['upper']:.2f} < open <= "
        f"{result['upper'] + MAX_ENTRY_EXTENSION * result['box_height']:.2f}"
        if side == "LONG"
        else f"{result['lower'] - MAX_ENTRY_EXTENSION * result['box_height']:.2f} "
        f"<= open < {result['lower']:.2f}"
    )
    return (
        f"{index}. {ticker:<6} | {side} | width {result['width_atr']:.2f} ATR | "
        f"turnover {result['turnover_ratio']:.2f}x | raw {result['raw_score']:+.2f} | "
        f"neutral {neutralized_score:+.2f} | rank {rank_score:+.2f} | "
        f"alpha weight {weight * 100:.1f}% | open gate {gate}"
    )


def scan_total_universe(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    progress_callback=None,
    report_artifacts=None,
    fixed_exit_sessions=None,
):
    """Scan the local universe and append the standard fixed IS/OOS report."""
    rows_by_side = {"LONG": [], "SHORT": []}
    failed_tickers = []
    raw_scores = {}
    candidates_by_ticker = {}
    prices_by_ticker = {
        ticker: _load_ohlcv(ticker, period=core_backtest_periods.HISTORY_PERIOD)
        for ticker in universe_list
    }
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            result = analyze_box_range_new(ticker, df=prices_by_ticker[ticker])
            succeeded = result["status"] not in ("DATA_INSUFFICIENT", "ERROR")
            if not succeeded:
                failed_tickers.append(ticker)
                continue
            raw_score = float(result.get("raw_score", 0.0))
            raw_scores[ticker] = raw_score
            if result["status"] in ("LONG_BREAKOUT", "SHORT_BREAKOUT"):
                rows_by_side[result["side"]].append((ticker, result, raw_score))
        finally:
            _notify_progress(
                progress_callback,
                "CURRENT_SCAN",
                completed,
                len(universe_list),
                ticker,
                succeeded,
            )

    neutralization = core_cross_sectional_neutralization.normalize_mode(neutralization)
    neutralized = core_cross_sectional_neutralization.neutralize_scores(
        raw_scores, neutralization
    )
    position_mode = core_position_mode.normalize_mode(position_mode)
    ranked_rows = {}
    pairs = []
    for side in ("LONG", "SHORT"):
        allowed = (
            core_position_mode.allows_long(position_mode)
            if side == "LONG"
            else core_position_mode.allows_short(position_mode)
        )
        rows = []
        if allowed:
            for ticker, result, raw_score in rows_by_side[side]:
                neutral_score = neutralized.get(ticker, raw_score)
                if neutralization != "NONE" and (
                    (side == "LONG" and neutral_score <= 0)
                    or (side == "SHORT" and neutral_score >= 0)
                ):
                    continue
                rows.append((ticker, result, raw_score, neutral_score))
                pairs.append(
                    (
                        Signal(
                            strategy=STRATEGY_KEY,
                            ticker=ticker,
                            side=side,
                            signal_date="LATEST_CLOSE",
                            raw_score=raw_score,
                            status=result["status"],
                            setup=result["setup"],
                            structure=result["structure"],
                            entry_execution="NEXT_OPEN",
                        ),
                        neutral_score,
                    )
                )
        ranked_rows[side] = rows

    scored = {
        item.signal.ticker: item
        for item in core_signal_scoring.score_signal_cohort(pairs)
    }
    for side in ("LONG", "SHORT"):
        ranked_rows[side] = [
            (*row, scored[row[0]].rank_score, scored[row[0]].requested_weight)
            for row in ranked_rows[side]
            if row[0] in scored
        ]
        ranked_rows[side].sort(
            key=lambda row: (row[4], row[2]), reverse=side == "LONG"
        )

    report = (
        "**Box Range (New one) Scan Report**\n"
        "===================================\n\n"
        f"{core_cross_sectional_neutralization.report_line(neutralization, raw_scores)}\n"
        f"{core_position_mode.report_line(position_mode)}\n"
        f"Rule: prior {BOX_BARS}-bar frozen box <= {MAX_WIDTH_ATR:.1f} ATR; "
        f"turnover >= {MIN_TURNOVER_RATIO:.1f}x; close breakout and bounded next open.\n\n"
    )
    for side in ("LONG", "SHORT"):
        report += f"**{side} Candidates (next open)**\n"
        report += (
            "\n".join(
                _candidate_row(index, ticker, result, neutral_score, rank, weight)
                for index, (
                    ticker,
                    result,
                    _,
                    neutral_score,
                    rank,
                    weight,
                ) in enumerate(ranked_rows[side][:10], 1)
            )
            or "None"
        )
        report += "\n\n"

    def cached_backtest(ticker, **kwargs):
        if ticker not in candidates_by_ticker:
            candidates_by_ticker[ticker] = _precompute_candidates(
                prices_by_ticker[ticker]
            )
        return run_box_backtest(
            ticker,
            df=prices_by_ticker[ticker],
            precomputed_candidates=candidates_by_ticker[ticker],
            **kwargs,
        )

    common = {
        "period": core_backtest_periods.HISTORY_PERIOD,
        "neutralization": neutralization,
        "position_mode": position_mode,
        "portfolio_accounting": True,
        "side_filter": core_position_mode.side_filter(position_mode),
        "fixed_exit_sessions": fixed_exit_sessions,
    }
    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        cached_backtest,
        progress_callback=_stage_progress_callback(progress_callback, "IS_BACKTEST"),
        **common,
        **core_backtest_periods.selection_window(),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        cached_backtest,
        progress_callback=_stage_progress_callback(progress_callback, "OOS_BACKTEST"),
        **common,
        **core_backtest_periods.oos_window(),
    )
    report += core_universe_backtest.format_fixed_is_oos_backtest(
        selection_result,
        oos_result,
        entry_text=(
            f"prior {BOX_BARS} completed bars form a <= {MAX_WIDTH_ATR:.1f} ATR box; "
            f"signal turnover >= {MIN_TURNOVER_RATIO:.1f}x prior {TURNOVER_BARS}d mean; "
            "close outside the frozen boundary; bounded next-open entry"
        ),
        exit_text=(
            f"close at D+{fixed_exit_sessions}; native exits disabled; incomplete D+N holdings excluded"
            if fixed_exit_sessions is not None
            else "full exit at frozen 1x box target; boundary failure exits next open; "
            f"otherwise {TIME_EXIT_SESSIONS}th session close or evaluation-end mark"
        ),
        strategy_key="box_range_new",
        report_artifacts=report_artifacts,
    )
    if failed_tickers:
        report += "\n**Data failed / insufficient**\n`" + ", ".join(
            sorted(set(failed_tickers))
        ) + "`"
    return report


def scan_box_range_new(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    progress_callback=None,
    report_artifacts=None,
    fixed_exit_sessions=None,
):
    return scan_total_universe(
        universe_list,
        neutralization=neutralization,
        position_mode=position_mode,
        progress_callback=progress_callback,
        report_artifacts=report_artifacts,
        fixed_exit_sessions=fixed_exit_sessions,
    )
