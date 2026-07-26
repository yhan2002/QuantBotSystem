# File name: strategy_box_range.py
"""Directional range/channel strategy.

Signals use only completed bars through the signal-day close and enter no
earlier than the next open. The prior fixed 20-day two-sided reclaim engine
remains available in the deployment folder; Beta intentionally uses this V2.
"""

import numpy as np
import pandas as pd

import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_market_data_store
import core_position_mode
import core_universe_backtest


CHANNEL_LOOKBACK_DAYS = 40
CHANNEL_LOWER_QUANTILE = 0.10
CHANNEL_UPPER_QUANTILE = 0.90
FLAT_MAX_SLOPE_RATIO = 0.35
TREND_MIN_SLOPE_RATIO = 0.75
MIN_TREND_EFFICIENCY = 0.20
MIN_CHANNEL_CONTAINMENT = 0.85
MIN_CHANNEL_WIDTH_ATR = 4.0
MAX_CHANNEL_WIDTH_ATR = 12.0
TOUCH_TOLERANCE_ATR = 0.50
MIN_BOUNDARY_TOUCHES = 2
MIN_TOUCH_SPACING_DAYS = 5
MIN_REBOUND_CLOSE_LOCATION = 0.60
VOLUME_LOOKBACK_DAYS = 20
VOLUME_SURGE_MULTIPLE = 2.0
TIME_EXIT_DAYS = 10
POST_FIRST_TARGET_EXIT_DAYS = 10


def _normalize_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in df.columns for column in required):
        return pd.DataFrame()
    return df.dropna(subset=required)


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


def _load_ohlcv(ticker, period="3mo", allow_download=False):
    df = core_market_data_store.get_price_data(
        ticker, period_days=_period_to_rows(period)
    )
    if not df.empty:
        return _normalize_ohlcv(df)
    if allow_download:
        core_market_data_store.ensure_price_data([ticker], min_period="1y")
        return _normalize_ohlcv(
            core_market_data_store.get_price_data(
                ticker, period_days=_period_to_rows(period)
            )
        )
    return pd.DataFrame()


def _average_true_range(history, period=20):
    high = history["High"].to_numpy(dtype=float)
    low = history["Low"].to_numpy(dtype=float)
    close = history["Close"].to_numpy(dtype=float)
    previous_close = np.concatenate(([close[0]], close[:-1]))
    true_range = np.maximum.reduce(
        [high - low, np.abs(high - previous_close), np.abs(low - previous_close)]
    )
    value = float(np.mean(true_range[-min(period, len(true_range)) :]))
    return value if np.isfinite(value) and value > 0 else float("nan")


def _spaced_touch_count(mask, minimum_spacing=MIN_TOUCH_SPACING_DAYS):
    count = 0
    previous = -minimum_spacing
    for index in np.flatnonzero(mask):
        if index - previous >= minimum_spacing:
            count += 1
            previous = int(index)
    return count


def _classify_structure(slope_ratio, trend_efficiency):
    if abs(slope_ratio) <= FLAT_MAX_SLOPE_RATIO:
        return "FLAT"
    if (
        slope_ratio >= TREND_MIN_SLOPE_RATIO
        and trend_efficiency >= MIN_TREND_EFFICIENCY
    ):
        return "RISING"
    if (
        slope_ratio <= -TREND_MIN_SLOPE_RATIO
        and trend_efficiency >= MIN_TREND_EFFICIENCY
    ):
        return "FALLING"
    return "TRANSITION"


def _channel_stats(history, assume_clean=False):
    """Fit a robust parallel channel to completed history only."""
    if not assume_clean:
        history = _normalize_ohlcv(history)
    if len(history) < 3:
        return None

    high = history["High"].to_numpy(dtype=float)
    low = history["Low"].to_numpy(dtype=float)
    close = history["Close"].to_numpy(dtype=float)
    base_price = float(close[0])
    if not np.isfinite(base_price) or base_price <= 0:
        return None

    x = np.arange(len(history), dtype=float)
    normalized_mid = ((high + low) / 2.0) / base_price
    slope_normalized, intercept_normalized = np.polyfit(x, normalized_mid, 1)
    center_normalized = intercept_normalized + slope_normalized * x
    lower_residual = low / base_price - center_normalized
    upper_residual = high / base_price - center_normalized
    lower_offset = float(np.quantile(lower_residual, CHANNEL_LOWER_QUANTILE))
    upper_offset = float(np.quantile(upper_residual, CHANNEL_UPPER_QUANTILE))
    if not np.isfinite(lower_offset + upper_offset) or upper_offset <= lower_offset:
        return None

    lower_line = (center_normalized + lower_offset) * base_price
    center_line = center_normalized * base_price
    upper_line = (center_normalized + upper_offset) * base_price
    channel_width = float(upper_line[-1] - lower_line[-1])
    if channel_width <= 0:
        return None

    channel_slope = float(slope_normalized * base_price)
    fitted_move = channel_slope * (len(history) - 1)
    slope_ratio = fitted_move / channel_width
    total_path = float(np.abs(np.diff(close)).sum())
    trend_efficiency = (
        abs(float(close[-1] - close[0])) / total_path if total_path > 0 else 0.0
    )
    structure = _classify_structure(slope_ratio, trend_efficiency)
    atr = _average_true_range(history)
    width_atr = channel_width / atr if np.isfinite(atr) and atr > 0 else float("inf")
    containment = float(
        ((close >= lower_line) & (close <= upper_line)).mean()
    )
    touch_tolerance = atr * TOUCH_TOLERANCE_ATR
    lower_touch_mask = (low <= lower_line + touch_tolerance) & (
        close >= lower_line
    )
    upper_touch_mask = (high >= upper_line - touch_tolerance) & (
        close <= upper_line
    )
    lower_touches = _spaced_touch_count(lower_touch_mask)
    upper_touches = _spaced_touch_count(upper_touch_mask)

    shared_quality_reasons = []
    if containment < MIN_CHANNEL_CONTAINMENT:
        shared_quality_reasons.append("low containment")
    if not MIN_CHANNEL_WIDTH_ATR <= width_atr <= MAX_CHANNEL_WIDTH_ATR:
        shared_quality_reasons.append("width outside ATR range")
    long_quality_reasons = list(shared_quality_reasons)
    short_quality_reasons = list(shared_quality_reasons)
    if lower_touches < MIN_BOUNDARY_TOUCHES:
        long_quality_reasons.append("insufficient spaced lower touches")
    if upper_touches < MIN_BOUNDARY_TOUCHES:
        short_quality_reasons.append("insufficient spaced upper touches")

    containment_component = min(containment / MIN_CHANNEL_CONTAINMENT, 1.0)
    long_touch_component = min(lower_touches / 3.0, 1.0)
    short_touch_component = min(upper_touches / 3.0, 1.0)
    width_component = max(
        0.0,
        1.0
        - abs(width_atr - (MIN_CHANNEL_WIDTH_ATR + MAX_CHANNEL_WIDTH_ATR) / 2.0)
        / ((MAX_CHANNEL_WIDTH_ATR - MIN_CHANNEL_WIDTH_ATR) / 2.0),
    )
    trend_component = (
        min(trend_efficiency / 0.50, 1.0) if structure == "RISING" else 1.0
    )
    common_quality_score = (
        0.35 * containment_component
        + 0.20 * width_component
        + 0.15 * trend_component
    )
    long_quality_score = common_quality_score + 0.30 * long_touch_component
    short_quality_score = common_quality_score + 0.30 * short_touch_component

    return {
        "structure": structure,
        "lowest": float(lower_line[-1]),
        "mid": float(center_line[-1]),
        "highest": float(upper_line[-1]),
        "channel_slope": channel_slope,
        "channel_width": channel_width,
        "box_width_pct": channel_width / float(lower_line[-1]),
        "slope_ratio": float(slope_ratio),
        "trend_efficiency": float(trend_efficiency),
        "containment_ratio": containment,
        "atr": atr,
        "width_atr": float(width_atr),
        "lower_touches": lower_touches,
        "upper_touches": upper_touches,
        "long_quality_ok": not long_quality_reasons,
        "short_quality_ok": not short_quality_reasons,
        "long_quality_reasons": long_quality_reasons,
        "short_quality_reasons": short_quality_reasons,
        "quality_ok": not shared_quality_reasons,
        "quality_reasons": shared_quality_reasons,
        "long_quality_score": float(long_quality_score),
        "short_quality_score": float(short_quality_score),
        "quality_score": float(
            max(long_quality_score, short_quality_score)
        ),
    }


def _project_levels(stats, steps):
    shift = stats["channel_slope"] * steps
    lower = stats["lowest"] + shift
    midpoint = stats["mid"] + shift
    upper = stats["highest"] + shift
    if stats.get("setup") == "RISING_PULLBACK":
        prior_high = stats["prior_swing_high"]
        first_target = max(midpoint, min(prior_high, upper))
    elif stats.get("setup") == "FALLING_RALLY":
        prior_low = stats["prior_swing_low"]
        first_target = min(midpoint, max(prior_low, lower))
    else:
        first_target = midpoint
    return {
        "lowest": lower,
        "mid": midpoint,
        "highest": upper,
        "first_target": first_target,
    }


def _turnover_ratio(history, current):
    volume = history["Volume"].to_numpy(dtype=float)[-VOLUME_LOOKBACK_DAYS:]
    close = history["Close"].to_numpy(dtype=float)[-VOLUME_LOOKBACK_DAYS:]
    average = float(np.mean(volume * close))
    current_turnover = float(current["Volume"] * current["Close"])
    if not average or not np.isfinite(average):
        return float("inf")
    return current_turnover / average


def _close_location(current):
    day_range = float(current["High"] - current["Low"])
    if day_range <= 0:
        return 0.0
    return float((current["Close"] - current["Low"]) / day_range)


def _entry_signal(history, current, assume_clean=False):
    """Return a close-known directional V2 signal and frozen channel."""
    stats = _channel_stats(history, assume_clean=assume_clean)
    if stats is None:
        return "CHANNEL_UNQUALIFIED", {}

    signal_levels = _project_levels(stats, 1)
    stats.update(signal_levels)
    stats["current_price"] = float(current["Close"])
    stats["turnover_ratio"] = _turnover_ratio(history, current)
    stats["close_location"] = _close_location(current)
    swing_window = min(20, len(history))
    stats["prior_swing_high"] = float(
        np.max(history["High"].to_numpy(dtype=float)[-swing_window:])
    )
    stats["prior_swing_low"] = float(
        np.min(history["Low"].to_numpy(dtype=float)[-swing_window:])
    )

    if stats["structure"] == "TRANSITION":
        return "TRANSITION_NO_TRADE", stats
    if stats["turnover_ratio"] >= VOLUME_SURGE_MULTIPLE:
        return "VOLUME_VETO", stats

    lower = stats["lowest"]
    midpoint = stats["mid"]
    upper = stats["highest"]
    close = float(current["Close"])
    low = float(current["Low"])
    high = float(current["High"])

    if stats["structure"] == "FLAT":
        long_signal = (
            stats["long_quality_ok"]
            and low < lower
            and lower < close < midpoint
            and stats["close_location"] >= MIN_REBOUND_CLOSE_LOCATION
        )
        if long_signal:
            stats["setup"] = "RANGE_RECLAIM"
            stats["side"] = "LONG"
            stats["quality_score"] = stats["long_quality_score"]
            stats["first_target"] = midpoint
            return "LONG_RANGE_RECLAIM", stats
        short_signal = (
            stats["short_quality_ok"]
            and high > upper
            and midpoint < close < upper
            and stats["close_location"]
            <= 1.0 - MIN_REBOUND_CLOSE_LOCATION
        )
        if short_signal:
            stats["setup"] = "RANGE_RECLAIM"
            stats["side"] = "SHORT"
            stats["quality_score"] = stats["short_quality_score"]
            stats["first_target"] = midpoint
            return "SHORT_RANGE_RECLAIM", stats
        if not stats["long_quality_ok"] and not stats["short_quality_ok"]:
            return "CHANNEL_UNQUALIFIED", stats
    elif stats["structure"] == "RISING":
        if not stats["long_quality_ok"]:
            return "CHANNEL_UNQUALIFIED", stats
        support_tolerance = stats["atr"] * TOUCH_TOLERANCE_ATR
        long_signal = (
            low <= lower + support_tolerance
            and lower < close < upper
            and close > float(current["Open"])
            and stats["close_location"] >= MIN_REBOUND_CLOSE_LOCATION
        )
        if long_signal:
            stats["setup"] = "RISING_PULLBACK"
            stats["side"] = "LONG"
            stats["quality_score"] = stats["long_quality_score"]
            stats["first_target"] = max(
                midpoint, min(stats["prior_swing_high"], upper)
            )
            if close < stats["first_target"]:
                return "LONG_TREND_PULLBACK", stats
    elif stats["structure"] == "FALLING":
        if not stats["short_quality_ok"]:
            return "CHANNEL_UNQUALIFIED", stats
        resistance_tolerance = stats["atr"] * TOUCH_TOLERANCE_ATR
        short_signal = (
            high >= upper - resistance_tolerance
            and lower < close < upper
            and close < float(current["Open"])
            and stats["close_location"]
            <= 1.0 - MIN_REBOUND_CLOSE_LOCATION
        )
        if short_signal:
            stats["setup"] = "FALLING_RALLY"
            stats["side"] = "SHORT"
            stats["quality_score"] = stats["short_quality_score"]
            stats["first_target"] = min(
                midpoint, max(stats["prior_swing_low"], lower)
            )
            if close > stats["first_target"]:
                return "SHORT_TREND_RALLY", stats
    return "CHANNEL_QUALIFIED", stats


def _precompute_entry_candidates(df, lookback_days=CHANNEL_LOOKBACK_DAYS):
    """Precompute V2 signals without using the signal bar in channel fitting."""
    row_count = len(df)
    output = {
        "eligible": np.zeros(row_count, dtype=bool),
        "side": np.full(row_count, "", dtype=object),
        "status": np.full(row_count, "", dtype=object),
        "stats": np.full(row_count, None, dtype=object),
    }
    for index in range(lookback_days, row_count):
        history = df.iloc[index - lookback_days : index]
        status, stats = _entry_signal(
            history, df.iloc[index], assume_clean=True
        )
        output["status"][index] = status
        output["stats"][index] = stats
        output["eligible"][index] = status in (
            "LONG_RANGE_RECLAIM",
            "LONG_TREND_PULLBACK",
            "SHORT_RANGE_RECLAIM",
            "SHORT_TREND_RALLY",
        )
        output["side"][index] = stats.get("side", "")
    return output


def _candidate_prefix(candidates, row_count):
    """Return an index-aligned prefix or None when a cache is incompatible."""
    required = ("eligible", "side", "status", "stats")
    if not candidates or any(key not in candidates for key in required):
        return None
    if any(len(candidates[key]) < row_count for key in required):
        return None
    return {key: candidates[key][:row_count] for key in required}


def _valid_next_open(entry_price, stats, side):
    levels = _project_levels(stats, 1)
    if side == "LONG":
        return levels["lowest"] < entry_price < levels["first_target"]
    return levels["first_target"] < entry_price < levels["highest"]


def _candidate_score(stats, side):
    event_distance = (
        stats["current_price"] - stats["lowest"]
        if side == "LONG"
        else stats["highest"] - stats["current_price"]
    )
    event_component = max(
        0.0, min(event_distance / max(stats["atr"], 1e-12), 1.0)
    )
    structure_component = (
        min(stats["trend_efficiency"] / 0.50, 1.0)
        if stats["structure"] in ("RISING", "FALLING")
        else 1.0 - min(abs(stats["slope_ratio"]) / FLAT_MAX_SLOPE_RATIO, 1.0)
    )
    magnitude = float(
        0.60 * stats["quality_score"]
        + 0.20 * event_component
        + 0.20 * structure_component
    )
    return magnitude if side == "LONG" else -magnitude


def analyze_box_range(
    ticker, lookback_days=CHANNEL_LOOKBACK_DAYS, df=None
):
    """Analyze the latest completed bar for a directional V2 signal."""
    try:
        df = (
            _load_ohlcv(ticker, period="6mo")
            if df is None
            else _normalize_ohlcv(df)
        )
        if df.empty or len(df) < lookback_days + 1:
            return {"status": "DATA_INSUFFICIENT"}

        history = df.iloc[-lookback_days - 1 : -1]
        current = df.iloc[-1]
        status, stats = _entry_signal(history, current, assume_clean=True)
        if not stats:
            return {"status": status, "message": "channel could not be estimated"}

        channel_text = (
            f"{stats['lowest']:,.2f}~{stats['highest']:,.2f}"
        )
        if status == "LONG_RANGE_RECLAIM":
            message = (
                f"flat range lower reclaim; channel {channel_text}, "
                f"slope {stats['slope_ratio']:+.2f}w, quality {stats['quality_score']:.2f}"
            )
        elif status == "LONG_TREND_PULLBACK":
            message = (
                f"rising-channel support rebound; channel {channel_text}, "
                f"slope {stats['slope_ratio']:+.2f}w, quality {stats['quality_score']:.2f}"
            )
        elif status == "SHORT_RANGE_RECLAIM":
            message = (
                f"flat range upper reclaim; channel {channel_text}, "
                f"slope {stats['slope_ratio']:+.2f}w, quality {stats['quality_score']:.2f}"
            )
        elif status == "SHORT_TREND_RALLY":
            message = (
                f"falling-channel resistance rejection; channel {channel_text}, "
                f"slope {stats['slope_ratio']:+.2f}w, quality {stats['quality_score']:.2f}"
            )
        elif status == "VOLUME_VETO":
            message = (
                f"qualified {stats['structure'].lower()} channel, but turnover "
                f"{stats['turnover_ratio']:.1f}x blocks entry"
            )
        elif status == "CHANNEL_UNQUALIFIED":
            reasons = ", ".join(
                sorted(
                    set(
                        stats["long_quality_reasons"]
                        + stats["short_quality_reasons"]
                    )
                )
            ) or "event quality"
            message = f"channel rejected: {reasons}"
        elif status == "TRANSITION_NO_TRADE":
            message = "ambiguous transition structure; no trade"
        else:
            message = (
                f"qualified {stats['structure'].lower()} channel; "
                "waiting for its long entry event"
            )
        return {"status": status, "message": message, **stats}
    except Exception as exc:
        return {"status": "ERROR", "message": str(exc)}


def _stop_fill(side, day_open, level):
    return min(day_open, level) if side == "LONG" else max(day_open, level)


def _pnl_pct(side, exit_price, entry_price):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (exit_price - entry_price) / entry_price * 100.0


def _stop_hit(side, day, level):
    return (
        float(day["Low"]) <= level
        if side == "LONG"
        else float(day["High"]) >= level
    )


def _target_hit(side, day, level):
    return (
        float(day["High"]) >= level
        if side == "LONG"
        else float(day["Low"]) <= level
    )


def run_box_backtest(
    ticker,
    lookback_days=CHANNEL_LOOKBACK_DAYS,
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
):
    """Backtest directional V2 range and trend-channel setups."""
    try:
        df = (
            _load_ohlcv(ticker, period=period)
            if df is None
            else _normalize_ohlcv(df)
        )
        if evaluation_end:
            df = df.loc[: pd.Timestamp(evaluation_end)]
        if df.empty or len(df) < lookback_days + 2:
            result = (0.0, 0.0, 0, "N/A", [])
            return result if include_trades else result[:4]

        signal_start_index = (
            max(lookback_days, len(df) - evaluation_days)
            if evaluation_days
            else lookback_days
        )
        requested_start = (
            pd.Timestamp(evaluation_start)
            if evaluation_start
            else df.index[signal_start_index]
        )
        period_label = (
            f"{max(df.index[signal_start_index], requested_start).date()} "
            f"to {df.index[-1].date()}"
        )
        candidates = _candidate_prefix(precomputed_candidates, len(df))
        if candidates is None:
            candidates = _precompute_entry_candidates(df, lookback_days)
        trades = []
        trade_details = []
        position = None

        def close_trade(current_position):
            trades.append(current_position["realized"])
            trade_details.append(
                {
                    "side": current_position["side"],
                    "setup": current_position["stats"]["setup"],
                    "structure": current_position["stats"]["structure"],
                    "entry_date": current_position["entry_date"],
                    "entry_price": current_position["entry_price"],
                    "signal_score": current_position["signal_score"],
                    "expected_rr": current_position["expected_rr"],
                    "slope_ratio": current_position["stats"]["slope_ratio"],
                    "containment_ratio": current_position["stats"][
                        "containment_ratio"
                    ],
                    "width_atr": current_position["stats"]["width_atr"],
                    "exit_events": current_position["exit_events"],
                    "entry_execution": current_position["entry_execution"],
                    "signal_close": current_position["signal_close"],
                    "next_open": current_position["next_open"],
                    "signal_to_open_return_pct": current_position[
                        "signal_to_open_return_pct"
                    ],
                    "signal_to_fill_return_pct": current_position[
                        "signal_to_fill_return_pct"
                    ],
                    "return": current_position["realized"],
                }
            )

        for index in range(signal_start_index, len(df) - 1):
            day = df.iloc[index]
            if position is None:
                if (
                    evaluation_start
                    and df.index[index + 1] < pd.Timestamp(evaluation_start)
                ):
                    continue
                if not candidates["eligible"][index]:
                    continue

                stats = dict(candidates["stats"][index])
                side = candidates["side"][index]
                if side_filter not in (None, side):
                    continue
                execution = core_entry_execution.resolve_entry(
                    df,
                    index,
                    side,
                    mode=entry_execution,
                    adverse_gap_limit_pct=adverse_gap_limit_pct,
                )
                if execution is None or not _valid_next_open(
                    execution["next_open"], stats, side
                ):
                    continue

                entry_price = float(execution["price"])
                entry_levels = _project_levels(stats, 1)
                reward = max(
                    entry_levels["highest"] - entry_price
                    if side == "LONG"
                    else entry_price - entry_levels["lowest"],
                    0.0,
                )
                final_stop_distance = entry_price * 0.10
                position = {
                    "side": side,
                    "entry_price": entry_price,
                    "entry_index": index + 1,
                    "signal_index": index,
                    "entry_date": df.index[index + 1].date().isoformat(),
                    "entry_execution": execution["mode"],
                    "signal_close": execution["signal_close"],
                    "next_open": execution["next_open"],
                    "signal_to_open_return_pct": execution[
                        "signal_to_open_return_pct"
                    ],
                    "signal_to_fill_return_pct": execution[
                        "signal_to_fill_return_pct"
                    ],
                    "exit_start_index": (
                        index + 1
                        if execution["allow_entry_day_exit"]
                        else index + 2
                    ),
                    "stats": stats,
                    "signal_score": _candidate_score(stats, side),
                    "expected_rr": (
                        reward / final_stop_distance
                        if final_stop_distance > 0
                        else 0.0
                    ),
                    "remaining": 1.0,
                    "realized": 0.0,
                    "first_target_taken": False,
                    "first_target_index": None,
                    "first_stop_taken": False,
                    "exit_events": [],
                }
                continue

            if index < position["exit_start_index"]:
                continue

            entry = position["entry_price"]
            side = position["side"]
            remaining = position["remaining"]
            first_stop = entry * (0.93 if side == "LONG" else 1.07)
            final_stop = entry * (0.90 if side == "LONG" else 1.10)
            event_date = df.index[index].date().isoformat()

            # Conservative daily-bar rule: stops take priority over targets.
            if _stop_hit(side, day, final_stop):
                if not position["first_stop_taken"]:
                    half = remaining * 0.5
                    fill = _stop_fill(side, float(day["Open"]), first_stop)
                    position["realized"] += half * _pnl_pct(
                        side, fill, entry
                    )
                    position["exit_events"].append(
                        {
                            "date": event_date,
                            "reason": "-7% stop",
                            "weight": half,
                            "price": fill,
                        }
                    )
                    remaining -= half
                fill = _stop_fill(side, float(day["Open"]), final_stop)
                position["realized"] += remaining * _pnl_pct(
                    side, fill, entry
                )
                position["exit_events"].append(
                    {
                        "date": event_date,
                        "reason": "-10% stop",
                        "weight": remaining,
                        "price": fill,
                    }
                )
                close_trade(position)
                position = None
                continue

            if (
                not position["first_stop_taken"]
                and _stop_hit(side, day, first_stop)
            ):
                half = remaining * 0.5
                fill = _stop_fill(side, float(day["Open"]), first_stop)
                position["realized"] += half * _pnl_pct(side, fill, entry)
                position["exit_events"].append(
                    {
                        "date": event_date,
                        "reason": "-7% stop",
                        "weight": half,
                        "price": fill,
                    }
                )
                position["remaining"] -= half
                position["first_stop_taken"] = True

            projection_steps = index - position["signal_index"]
            levels = _project_levels(position["stats"], projection_steps)
            if (
                not position["first_target_taken"]
                and _target_hit(side, day, levels["first_target"])
            ):
                half = position["remaining"] * 0.5
                position["realized"] += half * _pnl_pct(
                    side, levels["first_target"], entry
                )
                position["exit_events"].append(
                    {
                        "date": event_date,
                        "reason": "first target 50%",
                        "weight": half,
                        "price": levels["first_target"],
                    }
                )
                position["remaining"] -= half
                position["first_target_taken"] = True
                position["first_target_index"] = index

            if (
                position["first_target_taken"]
                and _target_hit(
                    side,
                    day,
                    levels["highest"]
                    if side == "LONG"
                    else levels["lowest"],
                )
            ):
                final_target = (
                    levels["highest"]
                    if side == "LONG"
                    else levels["lowest"]
                )
                position["realized"] += position["remaining"] * _pnl_pct(
                    side, final_target, entry
                )
                position["exit_events"].append(
                    {
                        "date": event_date,
                        "reason": (
                            "channel upper target"
                            if side == "LONG"
                            else "channel lower target"
                        ),
                        "weight": position["remaining"],
                        "price": final_target,
                    }
                )
                close_trade(position)
                position = None
                continue

            held_days = index - position["entry_index"] + 1
            if (
                not position["first_target_taken"]
                and held_days >= TIME_EXIT_DAYS
            ):
                position["realized"] += position["remaining"] * _pnl_pct(
                    side, float(day["Close"]), entry
                )
                position["exit_events"].append(
                    {
                        "date": event_date,
                        "reason": "10d no-target exit",
                        "weight": position["remaining"],
                        "price": float(day["Close"]),
                    }
                )
                close_trade(position)
                position = None
                continue

            if position["first_target_taken"]:
                post_target_days = (
                    index - position["first_target_index"] + 1
                )
                if post_target_days >= POST_FIRST_TARGET_EXIT_DAYS:
                    position["realized"] += position["remaining"] * _pnl_pct(
                        side, float(day["Close"]), entry
                    )
                    position["exit_events"].append(
                        {
                            "date": event_date,
                            "reason": "10d post-target exit",
                            "weight": position["remaining"],
                            "price": float(day["Close"]),
                        }
                    )
                    close_trade(position)
                    position = None

        if not trades:
            result = (0.0, 0.0, 0, period_label, [])
            return result if include_trades else result[:4]
        average_return = sum(trades) / len(trades)
        win_rate = sum(value > 0 for value in trades) / len(trades) * 100.0
        result = (
            average_return,
            win_rate,
            len(trades),
            period_label,
            trade_details,
        )
        return result if include_trades else result[:4]
    except Exception:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]


def _candidate_row(index, ticker, result, neutralized_score):
    setup_labels = {
        "LONG_RANGE_RECLAIM": "flat lower reclaim",
        "LONG_TREND_PULLBACK": "rising pullback",
        "SHORT_RANGE_RECLAIM": "flat upper reclaim",
        "SHORT_TREND_RALLY": "falling rally rejection",
    }
    setup = setup_labels[result["status"]]
    side = result["side"]
    open_gate = (
        f"{result['lowest']:.2f} < open < {result['first_target']:.2f}"
        if side == "LONG"
        else f"{result['first_target']:.2f} < open < {result['highest']:.2f}"
    )
    return (
        f"{index}. {ticker:<6} | {setup} | slope {result['slope_ratio']:+.2f}w | "
        f"width {result['width_atr']:.1f} ATR | quality {result['quality_score']:.2f} | "
        f"turnover {result['turnover_ratio']:.1f}x | neutral {neutralized_score:+.2f} | "
        f"open gate {open_gate}"
    )


def scan_total_universe(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
):
    """Scan and backtest V2 with direction controlled by position_mode."""
    long_candidates = []
    short_candidates = []
    failed_tickers = []
    raw_scores = {}
    candidate_cache = {}
    price_data = {
        ticker: _load_ohlcv(
            ticker, period=core_backtest_periods.HISTORY_PERIOD
        )
        for ticker in universe_list
    }
    for ticker in universe_list:
        result = analyze_box_range(ticker, df=price_data[ticker])
        if result["status"] in ("DATA_INSUFFICIENT", "ERROR"):
            failed_tickers.append(ticker)
            continue
        raw_score = 0.0
        if result["status"] in (
            "LONG_RANGE_RECLAIM",
            "LONG_TREND_PULLBACK",
            "SHORT_RANGE_RECLAIM",
            "SHORT_TREND_RALLY",
        ):
            raw_score = _candidate_score(result, result["side"])
            target = (
                long_candidates
                if result["side"] == "LONG"
                else short_candidates
            )
            target.append((ticker, result, raw_score))
        raw_scores[ticker] = raw_score

    neutralized_scores = (
        core_cross_sectional_neutralization.neutralize_scores(
            raw_scores, neutralization
        )
    )
    neutralization = (
        core_cross_sectional_neutralization.normalize_mode(neutralization)
    )
    position_mode = core_position_mode.normalize_mode(position_mode)
    long_candidates = [
        (
            ticker,
            result,
            raw_score,
            neutralized_scores.get(ticker, raw_score),
        )
        for ticker, result, raw_score in long_candidates
        if neutralization == "NONE"
        or neutralized_scores.get(ticker, 0.0) > 0
    ]
    short_candidates = [
        (
            ticker,
            result,
            raw_score,
            neutralized_scores.get(ticker, raw_score),
        )
        for ticker, result, raw_score in short_candidates
        if neutralization == "NONE"
        or neutralized_scores.get(ticker, 0.0) < 0
    ]
    long_candidates.sort(key=lambda item: (item[3], item[2]), reverse=True)
    short_candidates.sort(key=lambda item: (item[3], item[2]))
    if not core_position_mode.allows_long(position_mode):
        long_candidates = []
    if not core_position_mode.allows_short(position_mode):
        short_candidates = []

    report = (
        "**Box Range V2 Scan Report**\n"
        "=============================\n\n"
    )
    report += (
        core_cross_sectional_neutralization.report_line(
            neutralization, raw_scores
        )
        + "\n"
    )
    report += core_position_mode.report_line(position_mode) + "\n"
    report += (
        "Structures: flat boundary reclaims, rising-channel long pullbacks, "
        "and falling-channel short rally rejections; transition channels are excluded.\n\n"
    )
    report += "**LONG Candidates (next open)**\n" + (
        "\n".join(
            _candidate_row(index, ticker, result, score)
            for index, (ticker, result, _, score) in enumerate(
                long_candidates[:10], 1
            )
        )
        or "None"
    )
    report += "\n\n**SHORT Candidates (next open)**\n" + (
        "\n".join(
            _candidate_row(index, ticker, result, score)
            for index, (ticker, result, _, score) in enumerate(
                short_candidates[:10], 1
            )
        )
        or "None"
    )

    def cached_box_backtest(ticker, **kwargs):
        if ticker not in candidate_cache:
            candidate_cache[ticker] = _precompute_entry_candidates(
                price_data[ticker], CHANNEL_LOOKBACK_DAYS
            )
        return run_box_backtest(
            ticker,
            df=price_data[ticker],
            precomputed_candidates=candidate_cache[ticker],
            **kwargs,
        )

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
            "prior 40 completed bars form a robust parallel channel; flat channels "
            "trade false-break reclaims in either direction, rising channels trade "
            "long support rebounds, and falling channels trade short resistance "
            "rejections; transition structures are excluded; containment >=85%, "
            "width 4~12 ATR, 2+ spaced same-side touches, turnover <2x; signal at "
            "close and entry at the next open"
        ),
        exit_text=(
            "50% at the setup-specific first target and balance at the projected "
            "channel upper line; 10d no-target exit; remaining position exits 10d "
            "after the first target; -7% half stop and -10% final stop; stop-first "
            "daily-bar convention"
        ),
        strategy_key="box_range",
    )
    if failed_tickers:
        report += (
            "\n**Data failed / insufficient**\n`"
            + ", ".join(sorted(set(failed_tickers)))
            + "`"
        )
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
