# File name: box_range_engine.py
# Box false-breakout/reclaim strategy. Signals use the close and enter next open.
import numpy as np
import pandas as pd

import core_market_data_store
import core_universe_backtest
import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_position_mode


BOX_MIN_WIDTH_PCT = 0.11
BOX_MAX_WIDTH_PCT = 0.20
BOUNCE_BUFFER_PCT = 0.01
VOLUME_LOOKBACK_DAYS = 20
VOLUME_SURGE_MULTIPLE = 2.0
TIME_EXIT_DAYS = 10
POST_CENTER_TIME_EXIT_DAYS = 10


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in df.columns for column in required):
        return pd.DataFrame()
    return df.dropna(subset=required).copy()


def _period_to_rows(period):
    return {"1mo": 35, "3mo": 90, "6mo": 130, "1y": 260, "2y": 540, "3y": 800, "4y": 1060}.get(period)


def _load_ohlcv(ticker, period="3mo", allow_download=False):
    df = core_market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
    if not df.empty:
        return _normalize_ohlcv(df)
    if allow_download:
        core_market_data_store.ensure_price_data([ticker], min_period="1y")
        return _normalize_ohlcv(
            core_market_data_store.get_price_data(ticker, period_days=_period_to_rows(period))
        )
    return pd.DataFrame()


def _box_stats(history):
    high = float(history["High"].max())
    low = float(history["Low"].min())
    width_pct = (high - low) / low if low > 0 else float("inf")
    return {
        "highest": high,
        "lowest": low,
        "mid": (high + low) / 2.0,
        "box_width_pct": width_pct,
    }


def _boundary_bounces(history, stats, bounce_buffer_pct=BOUNCE_BUFFER_PCT):
    lower = stats["lowest"]
    upper = stats["highest"]
    lower_bounces = ((history["Low"] <= lower * (1 + bounce_buffer_pct)) & (history["Close"] > lower)).sum()
    upper_bounces = ((history["High"] >= upper * (1 - bounce_buffer_pct)) & (history["Close"] < upper)).sum()
    return int(lower_bounces), int(upper_bounces)


def _turnover_ratio(history, current):
    average = (history["Volume"] * history["Close"]).tail(VOLUME_LOOKBACK_DAYS).mean()
    current_turnover = float(current["Volume"] * current["Close"])
    if not average or pd.isna(average):
        return float("inf")
    return current_turnover / float(average)


def _entry_signal(history, current, min_width_pct=BOX_MIN_WIDTH_PCT, max_width_pct=BOX_MAX_WIDTH_PCT):
    """Return a close-known false-breakout reclaim signal for the next open."""
    stats = _box_stats(history)
    lower_bounces, upper_bounces = _boundary_bounces(history, stats)
    turnover_ratio = _turnover_ratio(history, current)
    stats.update(
        current_price=float(current["Close"]),
        lower_bounces=lower_bounces,
        upper_bounces=upper_bounces,
        turnover_ratio=turnover_ratio,
    )

    if not min_width_pct <= stats["box_width_pct"] <= max_width_pct:
        return "NORMAL", stats
    if turnover_ratio >= VOLUME_SURGE_MULTIPLE:
        return "VOLUME_VETO", stats

    lower_reclaim = current["Low"] < stats["lowest"] and stats["lowest"] < current["Close"] <= stats["highest"]
    upper_reclaim = current["High"] > stats["highest"] and stats["lowest"] <= current["Close"] < stats["highest"]
    if lower_reclaim and lower_bounces >= 2:
        return "LONG_RECLAIM", stats
    if upper_reclaim and upper_bounces >= 2:
        return "SHORT_RECLAIM", stats
    return "BOX_QUALIFIED", stats


def _precompute_entry_candidates(
    df,
    lookback_days,
    min_width_pct=BOX_MIN_WIDTH_PCT,
    max_width_pct=BOX_MAX_WIDTH_PCT,
):
    """Precompute prior-window box signals without changing close-known semantics."""
    row_count = len(df)
    output = {
        "eligible": np.zeros(row_count, dtype=bool),
        "short_eligible": np.zeros(row_count, dtype=bool),
        "highest": np.full(row_count, np.nan),
        "lowest": np.full(row_count, np.nan),
        "mid": np.full(row_count, np.nan),
        "box_width_pct": np.full(row_count, np.nan),
        "lower_bounces": np.zeros(row_count, dtype=int),
        "upper_bounces": np.zeros(row_count, dtype=int),
        "turnover_ratio": np.full(row_count, np.inf),
    }
    if row_count <= lookback_days:
        return output

    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    volume = df["Volume"].to_numpy(dtype=float)

    # Window row 0 contains bars [0:lookback_days], so it belongs to current
    # bar lookback_days. The last raw window includes the current bar and is excluded.
    high_windows = np.lib.stride_tricks.sliding_window_view(high, lookback_days)[:-1]
    low_windows = np.lib.stride_tricks.sliding_window_view(low, lookback_days)[:-1]
    close_windows = np.lib.stride_tricks.sliding_window_view(close, lookback_days)[:-1]
    volume_windows = np.lib.stride_tricks.sliding_window_view(volume, lookback_days)[:-1]

    highest = high_windows.max(axis=1)
    lowest = low_windows.min(axis=1)
    midpoint = (highest + lowest) / 2.0
    width_pct = np.divide(
        highest - lowest,
        lowest,
        out=np.full_like(lowest, np.inf),
        where=lowest > 0,
    )
    lower_bounces = (
        (low_windows <= lowest[:, None] * (1 + BOUNCE_BUFFER_PCT))
        & (close_windows > lowest[:, None])
    ).sum(axis=1)
    upper_bounces = (
        (high_windows >= highest[:, None] * (1 - BOUNCE_BUFFER_PCT))
        & (close_windows < highest[:, None])
    ).sum(axis=1)

    turnover_window = min(VOLUME_LOOKBACK_DAYS, lookback_days)
    average_turnover = (
        volume_windows[:, -turnover_window:] * close_windows[:, -turnover_window:]
    ).mean(axis=1)
    current_slice = slice(lookback_days, row_count)
    current_turnover = volume[current_slice] * close[current_slice]
    turnover_ratio = np.divide(
        current_turnover,
        average_turnover,
        out=np.full_like(current_turnover, np.inf),
        where=(average_turnover != 0) & ~np.isnan(average_turnover),
    )

    long_eligible = (
        (width_pct >= min_width_pct)
        & (width_pct <= max_width_pct)
        & (turnover_ratio < VOLUME_SURGE_MULTIPLE)
        & (low[current_slice] < lowest)
        & (close[current_slice] > lowest)
        & (close[current_slice] <= highest)
        & (lower_bounces >= 2)
    )
    short_eligible = (
        (width_pct >= min_width_pct)
        & (width_pct <= max_width_pct)
        & (turnover_ratio < VOLUME_SURGE_MULTIPLE)
        & (high[current_slice] > highest)
        & (close[current_slice] >= lowest)
        & (close[current_slice] < highest)
        & (upper_bounces >= 2)
    )
    output["eligible"][current_slice] = long_eligible
    output["short_eligible"][current_slice] = short_eligible
    output["highest"][current_slice] = highest
    output["lowest"][current_slice] = lowest
    output["mid"][current_slice] = midpoint
    output["box_width_pct"][current_slice] = width_pct
    output["lower_bounces"][current_slice] = lower_bounces
    output["upper_bounces"][current_slice] = upper_bounces
    output["turnover_ratio"][current_slice] = turnover_ratio
    return output


def _pnl_pct(position, exit_price, entry_price):
    direction = 1.0 if position == "LONG" else -1.0
    return direction * (exit_price - entry_price) / entry_price * 100.0


def _stop_fill(position, day_open, level):
    return min(day_open, level) if position == "LONG" else max(day_open, level)


def _target_hit(position, day, level):
    return day["High"] >= level if position == "LONG" else day["Low"] <= level


def _stop_hit(position, day, level):
    return day["Low"] <= level if position == "LONG" else day["High"] >= level


def _valid_next_open(entry_price, stats, side):
    """Require the next open to remain in the reclaimed half of the box."""
    if side == "LONG":
        return stats["lowest"] < entry_price < stats["mid"]
    return stats["mid"] < entry_price < stats["highest"]


def analyze_box_range(ticker, lookback_days=20, df=None):
    """Live signal: prior box, today's intraday breach and close reclaim."""
    try:
        df = _load_ohlcv(ticker, period="3mo") if df is None else _normalize_ohlcv(df)
        if df.empty or len(df) < lookback_days + 1:
            return {"status": "DATA_INSUFFICIENT"}

        history = df.iloc[-lookback_days - 1:-1]
        current = df.iloc[-1]
        status, stats = _entry_signal(history, current)
        box_text = f"{stats['lowest']:,.2f}~{stats['highest']:,.2f}"
        if status == "LONG_RECLAIM":
            message = f"lower false-break reclaim; box {box_text}, lower bounces {stats['lower_bounces']}, turnover {stats['turnover_ratio']:.1f}x"
        elif status == "SHORT_RECLAIM":
            message = f"upper false-break reclaim; box {box_text}, upper bounces {stats['upper_bounces']}, turnover {stats['turnover_ratio']:.1f}x"
        elif status == "VOLUME_VETO":
            message = f"qualified box {box_text}, but turnover {stats['turnover_ratio']:.1f}x blocks entry"
        elif status == "BOX_QUALIFIED":
            message = f"qualified box {box_text}; waiting for intraday breach and close reclaim"
        else:
            message = "box width must be 11%~20%"
        return {"status": status, "message": message, **stats}
    except Exception as exc:
        return {"status": "ERROR", "message": str(exc)}


def run_box_backtest(
    ticker,
    lookback_days=20,
    side_filter=None,
    df=None,
    period="1y",
    include_trades=False,
    evaluation_days=None,
    evaluation_start=None,
    evaluation_end=None,
    entry_execution=core_entry_execution.NEXT_OPEN,
    adverse_gap_limit_pct=core_entry_execution.DEFAULT_ADVERSE_GAP_LIMIT_PCT,
):
    """Backtest the long-only reclaim strategy using only the signal-day close and next open.

    Intraday target/stop collisions are resolved stop-first. Gap-through stops use
    the opening price when it is worse than the stop level.
    """
    try:
        df = _load_ohlcv(ticker, period=period) if df is None else _normalize_ohlcv(df)
        if evaluation_end:
            df = df.loc[:pd.Timestamp(evaluation_end)]
        if df.empty or len(df) < lookback_days + 2:
            result = (0.0, 0.0, 0, "N/A", [])
            return result if include_trades else result[:4]
        signal_start_idx = (
            max(lookback_days, len(df) - evaluation_days)
            if evaluation_days
            else lookback_days
        )
        requested_start = pd.Timestamp(evaluation_start) if evaluation_start else df.index[signal_start_idx]
        period_label = f"{max(df.index[signal_start_idx], requested_start).date()} to {df.index[-1].date()}"
        entry_candidates = _precompute_entry_candidates(df, lookback_days)
        trades = []
        trade_details = []
        position = None

        def close_trade(current_position, exit_date):
            trades.append(current_position["realized"])
            trade_details.append(
                {
                    "side": current_position["side"],
                    "entry_date": current_position["entry_date"],
                        "entry_price": current_position["entry_price"],
                        "signal_score": current_position["signal_score"],
                        "exit_events": current_position["exit_events"],
                        "entry_execution": current_position["entry_execution"],
                        "signal_close": current_position["signal_close"],
                        "next_open": current_position["next_open"],
                        "signal_to_open_return_pct": current_position["signal_to_open_return_pct"],
                        "signal_to_fill_return_pct": current_position["signal_to_fill_return_pct"],
                    "return": current_position["realized"],
                }
            )

        for i in range(signal_start_idx, len(df) - 1):
            day = df.iloc[i]
            if position is None:
                if evaluation_start and df.index[i + 1] < pd.Timestamp(evaluation_start):
                    continue
                side = (
                    "LONG"
                    if entry_candidates["eligible"][i]
                    else "SHORT"
                    if entry_candidates["short_eligible"][i]
                    else None
                )
                if side and side_filter in (None, side):
                    stats = {
                        "highest": float(entry_candidates["highest"][i]),
                        "lowest": float(entry_candidates["lowest"][i]),
                        "mid": float(entry_candidates["mid"][i]),
                        "box_width_pct": float(entry_candidates["box_width_pct"][i]),
                        "current_price": float(day["Close"]),
                        "lower_bounces": int(entry_candidates["lower_bounces"][i]),
                        "upper_bounces": int(entry_candidates["upper_bounces"][i]),
                        "turnover_ratio": float(entry_candidates["turnover_ratio"][i]),
                    }
                    execution = core_entry_execution.resolve_entry(
                        df,
                        i,
                        side,
                        mode=entry_execution,
                        adverse_gap_limit_pct=adverse_gap_limit_pct,
                    )
                    if execution is None or not _valid_next_open(
                        execution["next_open"], stats, side
                    ):
                        continue
                    entry_price = execution["price"]
                    position = {
                        "side": side,
                        "entry_price": entry_price,
                        "entry_index": i + 1,
                        "entry_date": df.index[i + 1].date().isoformat(),
                        "entry_execution": execution["mode"],
                        "signal_close": execution["signal_close"],
                        "next_open": execution["next_open"],
                        "signal_to_open_return_pct": execution["signal_to_open_return_pct"],
                        "signal_to_fill_return_pct": execution["signal_to_fill_return_pct"],
                        "exit_start_index": i + 1 if execution["allow_entry_day_exit"] else i + 2,
                        "stats": stats,
                        "signal_score": (
                            (stats["highest"] - float(day["Close"]))
                            / (stats["highest"] - stats["lowest"])
                            if side == "LONG" and stats["highest"] > stats["lowest"]
                            else -(
                                (float(day["Close"]) - stats["lowest"])
                                / (stats["highest"] - stats["lowest"])
                            )
                            if side == "SHORT" and stats["highest"] > stats["lowest"]
                            else 0.0
                        ),
                        "remaining": 1.0,
                        "realized": 0.0,
                        "center_taken": False,
                        "center_index": None,
                        "first_stop_taken": False,
                        "exit_events": [],
                    }
                continue

            side = position["side"]
            if i < position["exit_start_index"]:
                continue
            entry = position["entry_price"]
            remaining = position["remaining"]
            first_stop = entry * (0.93 if side == "LONG" else 1.07)
            final_stop = entry * (0.90 if side == "LONG" else 1.10)

            # Conservative daily-bar convention: protective stops take priority.
            if _stop_hit(side, day, final_stop):
                if not position["first_stop_taken"]:
                    half = remaining * 0.5
                    fill = _stop_fill(side, float(day["Open"]), first_stop)
                    position["realized"] += half * _pnl_pct(side, fill, entry)
                    position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-7% stop", "weight": half, "price": fill})
                    remaining -= half
                fill = _stop_fill(side, float(day["Open"]), final_stop)
                position["realized"] += remaining * _pnl_pct(side, fill, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-10% stop", "weight": remaining, "price": fill})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue
            if not position["first_stop_taken"] and _stop_hit(side, day, first_stop):
                half = remaining * 0.5
                fill = _stop_fill(side, float(day["Open"]), first_stop)
                position["realized"] += half * _pnl_pct(side, fill, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "-7% stop", "weight": half, "price": fill})
                position["remaining"] -= half
                position["first_stop_taken"] = True
                remaining = position["remaining"]

            if not position["center_taken"] and _target_hit(side, day, position["stats"]["mid"]):
                half = position["remaining"] * 0.5
                position["realized"] += half * _pnl_pct(side, position["stats"]["mid"], entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "center 50%", "weight": half, "price": position["stats"]["mid"]})
                position["remaining"] -= half
                position["center_taken"] = True
                position["center_index"] = i

            if position["center_taken"] and _target_hit(side, day, position["stats"]["highest"] if side == "LONG" else position["stats"]["lowest"]):
                target = position["stats"]["highest"] if side == "LONG" else position["stats"]["lowest"]
                position["realized"] += position["remaining"] * _pnl_pct(side, target, entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "opposite edge", "weight": position["remaining"], "price": target})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue

            held_days = i - position["entry_index"] + 1
            if not position["center_taken"] and held_days >= TIME_EXIT_DAYS:
                position["realized"] += position["remaining"] * _pnl_pct(side, float(day["Close"]), entry)
                position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "10d time exit", "weight": position["remaining"], "price": float(day["Close"])})
                close_trade(position, df.index[i].date().isoformat())
                position = None
                continue
            if position["center_taken"]:
                post_center_days = i - position["center_index"] + 1
                if post_center_days >= POST_CENTER_TIME_EXIT_DAYS:
                    position["realized"] += position["remaining"] * _pnl_pct(side, float(day["Close"]), entry)
                    position["exit_events"].append({"date": df.index[i].date().isoformat(), "reason": "10d post-center exit", "weight": position["remaining"], "price": float(day["Close"])})
                    close_trade(position, df.index[i].date().isoformat())
                    position = None
                    continue
        if not trades:
            result = (0.0, 0.0, 0, period_label, [])
            return result if include_trades else result[:4]
        avg_return = sum(trades) / len(trades)
        win_rate = sum(pnl > 0 for pnl in trades) / len(trades) * 100.0
        result = (avg_return, win_rate, len(trades), period_label, trade_details)
        return result if include_trades else result[:4]
    except Exception:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]


def _box_row(idx, ticker, res, action, neutralized_score):
    return (
        f"{idx}. {ticker:<6} | {action} | width {res['box_width_pct'] * 100:.1f}% | "
        f"turnover {res['turnover_ratio']:.1f}x | neutral {neutralized_score:+.2f} | "
        f"open gate {res['lowest']:.2f} < open < {res['mid']:.2f}"
    )


def scan_total_universe(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
):
    long_reclaim, short_reclaim, failed_tickers = [], [], []
    raw_scores = {}
    price_data = {
        ticker: _load_ohlcv(ticker, period=core_backtest_periods.HISTORY_PERIOD)
        for ticker in universe_list
    }
    for ticker in universe_list:
        res = analyze_box_range(ticker, df=price_data[ticker])
        if res["status"] in ["DATA_INSUFFICIENT", "ERROR"]:
            failed_tickers.append(ticker)
            continue
        raw_score = 0.0
        if res["status"] == "LONG_RECLAIM":
            width = res["highest"] - res["lowest"]
            raw_score = (res["highest"] - res["current_price"]) / width if width > 0 else 0.0
            long_reclaim.append((ticker, res, raw_score))
        elif res["status"] == "SHORT_RECLAIM":
            width = res["highest"] - res["lowest"]
            raw_score = -((res["current_price"] - res["lowest"]) / width) if width > 0 else 0.0
            short_reclaim.append((ticker, res, raw_score))
        raw_scores[ticker] = raw_score

    neutralized_scores = core_cross_sectional_neutralization.neutralize_scores(
        raw_scores, neutralization
    )
    neutralization = core_cross_sectional_neutralization.normalize_mode(neutralization)
    position_mode = core_position_mode.normalize_mode(position_mode)
    long_reclaim = [
        (ticker, res, raw_score, neutralized_scores.get(ticker, raw_score))
        for ticker, res, raw_score in long_reclaim
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) > 0
    ]
    long_reclaim.sort(key=lambda item: (item[3], item[2]), reverse=True)
    short_reclaim = [
        (ticker, res, raw_score, neutralized_scores.get(ticker, raw_score))
        for ticker, res, raw_score in short_reclaim
        if neutralization == "NONE" or neutralized_scores.get(ticker, 0.0) < 0
    ]
    short_reclaim.sort(key=lambda item: (item[3], item[2]))
    if not core_position_mode.allows_long(position_mode):
        long_reclaim = []
    if not core_position_mode.allows_short(position_mode):
        short_reclaim = []

    report = "**Box Range Scan Report**\n=============================\n\n"
    report += core_cross_sectional_neutralization.report_line(neutralization, raw_scores) + "\n"
    report += core_position_mode.report_line(position_mode) + "\n"
    report += "Scope: today's candidates and entry-date IS/OOS cross-sections; current metadata labels.\n\n"
    report += "**LONG Candidates (next open)**\n" + ("\n".join(_box_row(i, ticker, res, "lower reclaim", score) for i, (ticker, res, _, score) in enumerate(long_reclaim[:10], 1)) or "None")
    report += "\n\n**SHORT Candidates (next open)**\n" + ("\n".join(_box_row(i, ticker, res, "upper reclaim", score) for i, (ticker, res, _, score) in enumerate(short_reclaim[:10], 1)) or "None")

    def cached_box_backtest(ticker, **kwargs):
        return run_box_backtest(ticker, df=price_data[ticker], **kwargs)

    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        cached_box_backtest,
        period=core_backtest_periods.HISTORY_PERIOD,
        neutralization=neutralization,
        side_filter=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.selection_window(),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        cached_box_backtest,
        period=core_backtest_periods.HISTORY_PERIOD,
        neutralization=neutralization,
        side_filter=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.oos_window(),
    )
    report += core_universe_backtest.format_fixed_is_oos_backtest(
        selection_result,
        oos_result,
        entry_text=(
            "prior 20-day box width 11%~20%, 2+ same-side bounces, intraday boundary breach then "
            "close reclaim, and turnover <2x its 20d average; enter only when next open remains "
            "inside the reclaimed half of the box"
        ),
        exit_text=(
            "50% at center and balance at opposite edge; 10d no-center exit; remaining position "
            "exits 10d after center if the opposite edge is not reached; -7% half stop and -10% final stop"
        ),
        strategy_key="box_range",
    )
    if failed_tickers:
        report += "\n**Data failed / insufficient**\n`" + ", ".join(sorted(set(failed_tickers))) + "`"
    return report


def scan_box_range(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
):
    return scan_total_universe(
        universe_list,
        neutralization=neutralization,
        position_mode=position_mode,
    )
