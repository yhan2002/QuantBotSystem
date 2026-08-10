# File name: value_reversion_engine.py
# Price/liquidity reversion: excess move, reversal confirmation, and scale-out exits.
import math

import pandas as pd

import core_market_data_store
import core_universe_backtest
import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_position_mode
import core_signal_scoring
from core_signal_model import Signal


REVERSION_SHOCK_DAYS = 5
REVERSION_VOL_WINDOW = 20
REVERSION_MA_WINDOW = 60
REVERSION_ATR_WINDOW = 14
REVERSION_TURNOVER_LOOKBACK = 20
REVERSION_LONG_THRESHOLD = -1.40
REVERSION_SHORT_THRESHOLD = 1.60
REVERSION_MAX_TURNOVER_RATIO = 2.00
REVERSION_LONG_CLOSE_LOCATION = 0.55
REVERSION_SHORT_CLOSE_LOCATION = 0.45
REVERSION_TREND_SLOPE_TOLERANCE = 0.001
REVERSION_PARTIAL_LEVEL = 0.50
REVERSION_NO_PROGRESS_DAYS = 3
REVERSION_MAX_HOLDING_DAYS = 10
REVERSION_STOP_ATR = 1.50

BRAIN_ALPHA_OFF = "OFF"
BRAIN_ALPHA_APAC_EXACT = "APAC_EXACT_FILTER"
BRAIN_ALPHA_APAC_UNIT_SAFE = "APAC_UNIT_SAFE_FILTER"
BRAIN_ALPHA_LOOKBACK = 252
BRAIN_ALPHA_HISTORY_DAYS = 1460
BRAIN_ALPHA_MIN_COHORT = 20
BRAIN_ALPHA_EPSILON = 1e-12
BRAIN_ALPHA_COUNTRY_POLICY = {
    BRAIN_ALPHA_OFF: {},
    BRAIN_ALPHA_APAC_EXACT: {
        "CH": "EXACT",
        "JP": "EXACT",
        "KR": "EXACT",
    },
    BRAIN_ALPHA_APAC_UNIT_SAFE: {
        "CH": "UNIT_SAFE",
        "JP": "UNIT_SAFE",
        "KR": "UNIT_SAFE",
    },
}


def normalize_brain_alpha_policy(policy):
    normalized = str(policy or BRAIN_ALPHA_OFF).strip().upper()
    if normalized not in BRAIN_ALPHA_COUNTRY_POLICY:
        raise ValueError(f"Unknown BRAIN alpha policy: {policy}")
    return normalized


def brain_alpha_variant_for_country(policy, listing_country):
    policy = normalize_brain_alpha_policy(policy)
    return BRAIN_ALPHA_COUNTRY_POLICY[policy].get(
        str(listing_country or "").strip().upper()
    )


def _close_asof(frame, signal_date):
    if frame is None or frame.empty or "Close" not in frame.columns:
        return pd.Series(dtype=float)
    close = pd.to_numeric(frame["Close"], errors="coerce").copy()
    close.index = pd.to_datetime(close.index, errors="coerce")
    close = close[~close.index.isna()].sort_index()
    close = close[~close.index.duplicated(keep="last")]
    cutoff = pd.Timestamp(signal_date)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    return close.loc[:cutoff]


def _backfilled_tail(series):
    values = pd.to_numeric(series, errors="coerce").ffill(limit=BRAIN_ALPHA_LOOKBACK)
    values = values.tail(BRAIN_ALPHA_LOOKBACK)
    if len(values) < BRAIN_ALPHA_LOOKBACK or values.isna().any():
        return pd.Series(dtype=float)
    return values.astype(float)


def _ts_rank_last(series, window):
    values = list(series.tail(window))
    if len(values) < window:
        return None
    last = values[-1]
    lower = sum(value < last for value in values)
    equal = sum(value == last for value in values)
    average_zero_based_rank = lower + (equal - 1.0) / 2.0
    return average_zero_based_rank / (len(values) - 1.0)


def _ts_zscore_last(series, window):
    values = series.tail(window)
    if len(values) < window:
        return None
    deviation = float(values.std(ddof=0))
    if not math.isfinite(deviation) or deviation <= 0.0:
        return None
    return (float(values.iloc[-1]) - float(values.mean())) / deviation


def _brain_alpha_inputs(frame, signal_date, variant):
    close = _close_asof(frame, signal_date)
    cutoff = pd.Timestamp(signal_date)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    if close.empty or close.index[-1].normalize() != cutoff.normalize():
        return None
    if variant == "EXACT":
        values = _backfilled_tail(close)
        if values.empty:
            return None
        rank = _ts_rank_last(values, 5)
        deviation = float(values.tail(126).std(ddof=0))
        if rank is None or not math.isfinite(deviation) or deviation <= 0.0:
            return None
        return -rank, -deviation
    if variant == "UNIT_SAFE":
        returns = _backfilled_tail(close.pct_change(fill_method=None))
        if returns.empty:
            return None
        slow = _ts_zscore_last(returns, 126)
        fast = _ts_zscore_last(returns, 5)
        if slow is None or fast is None:
            return None
        return -slow, -fast
    raise ValueError(f"Unknown BRAIN alpha variant: {variant}")


def _cross_sectional_residuals(
    inputs,
    *,
    intercept,
    min_cohort=BRAIN_ALPHA_MIN_COHORT,
    epsilon=BRAIN_ALPHA_EPSILON,
):
    if len(inputs) < min_cohort:
        return {}
    tickers = sorted(inputs)
    x_values = [inputs[ticker][0] for ticker in tickers]
    y_values = [inputs[ticker][1] for ticker in tickers]
    if intercept:
        x_mean = sum(x_values) / len(x_values)
        y_mean = sum(y_values) / len(y_values)
        denominator = sum((value - y_mean) ** 2 for value in y_values)
        if denominator <= epsilon:
            return {}
        beta = sum(
            (x_value - x_mean) * (y_value - y_mean)
            for x_value, y_value in zip(x_values, y_values)
        ) / denominator
        alpha = x_mean - beta * y_mean
    else:
        denominator = sum(value * value for value in y_values)
        if denominator <= epsilon:
            return {}
        beta = sum(
            x_value * y_value
            for x_value, y_value in zip(x_values, y_values)
        ) / denominator
        alpha = 0.0
    return {
        ticker: x_value - (alpha + beta * y_value)
        for ticker, x_value, y_value in zip(tickers, x_values, y_values)
    }


def brain_alpha_scores(
    prices_by_ticker,
    metadata_by_ticker,
    signal_date,
    policy,
    *,
    min_cohort=BRAIN_ALPHA_MIN_COHORT,
):
    policy = normalize_brain_alpha_policy(policy)
    if policy == BRAIN_ALPHA_OFF:
        return {}
    grouped = {}
    for ticker in sorted(prices_by_ticker):
        frame = prices_by_ticker[ticker]
        country = str(
            (metadata_by_ticker.get(ticker) or {}).get("listing_country") or ""
        ).strip().upper()
        variant = brain_alpha_variant_for_country(policy, country)
        if variant is None:
            continue
        inputs = _brain_alpha_inputs(frame, signal_date, variant)
        if inputs is not None:
            grouped.setdefault((country, variant), {})[ticker] = inputs
    scores = {}
    for country, variant in sorted(grouped):
        inputs = grouped[(country, variant)]
        scores.update(
            _cross_sectional_residuals(
                inputs,
                intercept=variant == "UNIT_SAFE",
                min_cohort=min_cohort,
            )
        )
    return scores


def filter_reversion_candidates(rows, alpha_scores, metadata_by_ticker, policy):
    policy = normalize_brain_alpha_policy(policy)
    if policy == BRAIN_ALPHA_OFF:
        return rows
    selected = []
    for row in rows:
        ticker = row["ticker"]
        country = (metadata_by_ticker.get(ticker) or {}).get("listing_country")
        variant = brain_alpha_variant_for_country(policy, country)
        alpha_score = alpha_scores.get(ticker)
        if variant is None or alpha_score is None:
            selected.append(row)
            continue
        side_matches = (
            row["side"] == "LONG" and alpha_score > BRAIN_ALPHA_EPSILON
        ) or (
            row["side"] == "SHORT" and alpha_score < -BRAIN_ALPHA_EPSILON
        )
        if side_matches:
            selected.append(
                {
                    **row,
                    "brain_alpha_policy": policy,
                    "brain_alpha_score": float(alpha_score),
                }
            )
    return selected


def apply_brain_alpha_policy(
    rows,
    prices_by_ticker,
    metadata_by_ticker,
    policy,
    *,
    date_key="signal_date",
):
    policy = normalize_brain_alpha_policy(policy)
    if policy == BRAIN_ALPHA_OFF:
        return rows
    cohorts = {}
    for row in rows:
        cohorts.setdefault(row.get(date_key), []).append(row)
    selected = []
    for signal_date, cohort in cohorts.items():
        scores = brain_alpha_scores(
            prices_by_ticker,
            metadata_by_ticker,
            signal_date,
            policy,
        )
        selected.extend(
            filter_reversion_candidates(
                cohort,
                scores,
                metadata_by_ticker,
                policy,
            )
        )
    return selected


def build_brain_alpha_trade_overlay(
    policy,
    *,
    price_loader=None,
    metadata_loader=None,
):
    policy = normalize_brain_alpha_policy(policy)
    if policy == BRAIN_ALPHA_OFF:
        return None
    price_loader = price_loader or core_market_data_store.get_price_data
    metadata_loader = metadata_loader or core_market_data_store.get_security_metadata
    prices_by_ticker = {}
    metadata_by_ticker = {}

    def overlay(rows, eligible_tickers):
        if not rows:
            return rows
        tickers = list(dict.fromkeys(eligible_tickers))
        missing_metadata = [ticker for ticker in tickers if ticker not in metadata_by_ticker]
        if missing_metadata:
            loaded = metadata_loader(missing_metadata)
            for ticker in missing_metadata:
                metadata = dict(loaded.get(ticker) or {})
                metadata["listing_country"] = metadata.get(
                    "listing_country"
                ) or core_market_data_store._infer_listing_country(ticker)
                metadata_by_ticker[ticker] = metadata
        for ticker in tickers:
            if ticker not in prices_by_ticker:
                try:
                    prices_by_ticker[ticker] = price_loader(
                        ticker,
                        period_days=BRAIN_ALPHA_HISTORY_DAYS,
                    )
                except TypeError:
                    prices_by_ticker[ticker] = price_loader(ticker)
        date_key = "signal_date"
        prepared_rows = rows
        if rows and all(not row.get("signal_date") for row in rows):
            date_key = "_brain_signal_date"
            prepared_rows = []
            for row in rows:
                frame = prices_by_ticker.get(row["ticker"])
                if frame is None or frame.empty:
                    prepared_rows.append(row)
                    continue
                prepared_rows.append(
                    {
                        **row,
                        date_key: pd.Timestamp(frame.index[-1]).date().isoformat(),
                    }
                )
        selected = apply_brain_alpha_policy(
            prepared_rows,
            prices_by_ticker,
            metadata_by_ticker,
            policy,
            date_key=date_key,
        )
        if date_key == "_brain_signal_date":
            return [
                {key: value for key, value in row.items() if key != date_key}
                for row in selected
            ]
        return selected

    return overlay


def _normalize_ohlcv(prices):
    if prices is None or prices.empty:
        return pd.DataFrame()
    if isinstance(prices.columns, pd.MultiIndex):
        prices = prices.copy()
        prices.columns = prices.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in prices.columns for column in required):
        return pd.DataFrame()
    return prices.dropna(subset=required).copy()


def _add_reversion_features(prices):
    prices = _normalize_ohlcv(prices)
    if prices.empty:
        return prices

    close = prices["Close"].astype(float)
    high = prices["High"].astype(float)
    low = prices["Low"].astype(float)
    volume = prices["Volume"].astype(float)
    daily_return = close.pct_change()
    volatility = daily_return.rolling(REVERSION_VOL_WINDOW).std(ddof=0)

    prices["SHOCK"] = close.pct_change(REVERSION_SHOCK_DAYS) / (
        volatility * math.sqrt(REVERSION_SHOCK_DAYS)
    )
    prices["MA60"] = close.rolling(REVERSION_MA_WINDOW).mean()
    prices["MA60_SLOPE"] = prices["MA60"].pct_change(5)
    prices["TURNOVER_RATIO"] = (volume * close) / (
        (volume * close).shift(1).rolling(REVERSION_TURNOVER_LOOKBACK).mean()
    )

    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    prices["ATR"] = true_range.rolling(REVERSION_ATR_WINDOW).mean()
    range_size = (high - low).replace(0, pd.NA)
    prices["CLOSE_LOCATION"] = ((close - low) / range_size).fillna(0.5)

    # Permit a close-confirmed reversal up to three sessions after the shock.
    recent_long_extreme = (
        prices["SHOCK"].shift(1).rolling(3).min() <= REVERSION_LONG_THRESHOLD
    )
    recent_short_extreme = (
        prices["SHOCK"].shift(1).rolling(3).max() >= REVERSION_SHORT_THRESHOLD
    )
    confirmation_long = (close > close.shift(1)) & (
        prices["CLOSE_LOCATION"] >= REVERSION_LONG_CLOSE_LOCATION
    ) & (close >= prices["Open"].astype(float))
    confirmation_short = (close < close.shift(1)) & (
        prices["CLOSE_LOCATION"] <= REVERSION_SHORT_CLOSE_LOCATION
    )
    liquid_enough = prices["TURNOVER_RATIO"] < REVERSION_MAX_TURNOVER_RATIO
    prices["LONG_SIGNAL"] = (
        recent_long_extreme
        & confirmation_long
        & liquid_enough
        & (prices["MA60_SLOPE"] >= -REVERSION_TREND_SLOPE_TOLERANCE)
    ).fillna(False)
    prices["SHORT_SIGNAL"] = (
        recent_short_extreme
        & confirmation_short
        & liquid_enough
        & (prices["MA60_SLOPE"] <= REVERSION_TREND_SLOPE_TOLERANCE)
    ).fillna(False)
    return prices


def analyze_value_reversion(ticker, allow_fundamental_download=True, df=None):
    """Return a confirmed price/liquidity reversion signal known at today's close."""
    del allow_fundamental_download
    prices = (
        core_market_data_store.get_price_data(ticker, period_days=130)
        if df is None
        else df
    )
    features = _add_reversion_features(prices)
    minimum_rows = REVERSION_MA_WINDOW + REVERSION_SHOCK_DAYS + 2
    if features.empty or len(features) < minimum_rows:
        return {"status": "DATA_INSUFFICIENT", "score": 0.0}

    latest = features.iloc[-1]
    recent_shocks = features["SHOCK"].iloc[-4:-1].dropna()
    previous_shock = float(recent_shocks.iloc[-1]) if not recent_shocks.empty else 0.0
    if bool(latest["LONG_SIGNAL"]):
        return {
            "status": "OK",
            "score": abs(float(recent_shocks.min())),
            "side": "LONG",
            "shock": previous_shock,
            "turnover_ratio": float(latest["TURNOVER_RATIO"]),
            "current_price": float(latest["Close"]),
        }
    if bool(latest["SHORT_SIGNAL"]):
        return {
            "status": "OK",
            "score": -abs(float(recent_shocks.max())),
            "side": "SHORT",
            "shock": previous_shock,
            "turnover_ratio": float(latest["TURNOVER_RATIO"]),
            "current_price": float(latest["Close"]),
        }
    return {"status": "NO_SIGNAL", "score": 0.0}


def _pnl_pct(side, exit_price, entry_price):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (exit_price - entry_price) / entry_price * 100.0


def _stop_hit(side, day, stop_price):
    return day["Low"] <= stop_price if side == "LONG" else day["High"] >= stop_price


def _stop_fill(side, day_open, stop_price):
    return min(day_open, stop_price) if side == "LONG" else max(day_open, stop_price)


def _post_partial_stop_price(side, entry_price, current_stop):
    """Protect a confirmed partial gain without weakening conservative gap fills."""
    return max(current_stop, entry_price) if side == "LONG" else min(current_stop, entry_price)


def _format_price(value):
    return f"{value:,.2f}" if value < 1000 else f"{value:,.0f}"


def _format_rows(items, long_side=True):
    if not items:
        return "None\n"
    direction = "LONG" if long_side else "SHORT"
    return "\n".join(
        f"{idx}. {row['ticker']:<8} | {direction} | price {_format_price(row['current_price'])} | "
        f"raw {row['score']:+.2f} | neutral {row['neutralized_score']:+.2f} | "
        f"rank {row['rank_score']:+.2f} | alpha weight {row['requested_weight'] * 100:.1f}% | "
        f"turnover {row['turnover_ratio']:.1f}x"
        for idx, row in enumerate(items, start=1)
    ) + "\n"


def run_reversion_backtest(
    ticker,
    side=None,
    period_days=260,
    include_trades=False,
    df=None,
    evaluation_days=None,
    evaluation_start=None,
    evaluation_end=None,
    entry_execution=core_entry_execution.NEXT_OPEN,
    adverse_gap_limit_pct=core_entry_execution.DEFAULT_ADVERSE_GAP_LIMIT_PCT,
):
    """Backtest confirmed reversion entries and B-style two-stage profit taking.

    All indicator exits are confirmed at the close and filled at the next open.
    ATR stops are intraday protective exits and use a conservative gap fill.
    """
    prices = core_market_data_store.get_price_data(ticker, period_days=period_days) if df is None else df
    if evaluation_end:
        prices = prices.loc[:pd.Timestamp(evaluation_end)]
    features = _add_reversion_features(prices)
    # MA60 needs five additional observations for its trend slope.
    warmup = REVERSION_MA_WINDOW + 5
    if features.empty or len(features) < warmup + 2:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]

    signal_start_idx = (
        max(warmup, len(features) - evaluation_days)
        if evaluation_days
        else warmup
    )
    requested_start = pd.Timestamp(evaluation_start) if evaluation_start else features.index[signal_start_idx]
    period_label = f"{max(features.index[signal_start_idx], requested_start).date()} to {features.index[-1].date()}"
    trades, trade_details = [], []
    position = None

    def add_exit(current_position, idx, reason, weight, price):
        current_position["realized"] += weight * _pnl_pct(
            current_position["side"], price, current_position["entry_price"]
        )
        current_position["remaining"] -= weight
        current_position["exit_events"].append(
            {
                "date": features.index[idx].date().isoformat(),
                "reason": reason,
                "weight": weight,
                "price": price,
            }
        )

    def finish_trade(current_position):
        trades.append(current_position["realized"])
        trade_details.append(
            {
                "side": current_position["side"],
                "signal_date": current_position["signal_date"],
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

    for idx in range(signal_start_idx, len(features)):
        day = features.iloc[idx]
        if position is not None and position.get("pending_exit"):
            pending = position.pop("pending_exit")
            fill = float(day["Open"])
            weight = position["remaining"] if pending["final"] else position["remaining"] * 0.5
            add_exit(position, idx, pending["reason"], weight, fill)
            if pending["final"]:
                finish_trade(position)
                position = None
                continue
            position["stop_price"] = _post_partial_stop_price(
                position["side"], position["entry_price"], position["stop_price"]
            )

        if position is None:
            if idx + 1 >= len(features):
                continue
            signal_side = "LONG" if bool(day["LONG_SIGNAL"]) else "SHORT" if bool(day["SHORT_SIGNAL"]) else None
            if signal_side is None or side not in (None, signal_side):
                continue
            entry_idx = idx + 1
            if evaluation_start and features.index[entry_idx] < pd.Timestamp(evaluation_start):
                continue
            execution = core_entry_execution.resolve_entry(
                features,
                idx,
                signal_side,
                mode=entry_execution,
                adverse_gap_limit_pct=adverse_gap_limit_pct,
            )
            if execution is None:
                continue
            entry_price = execution["price"]
            atr = float(day["ATR"])
            if entry_price <= 0 or not pd.notna(atr) or atr <= 0:
                continue
            stop_price = entry_price - REVERSION_STOP_ATR * atr if signal_side == "LONG" else entry_price + REVERSION_STOP_ATR * atr
            position = {
                "side": signal_side,
                "entry_price": entry_price,
                "entry_idx": entry_idx,
                "signal_date": features.index[idx].date().isoformat(),
                "entry_date": features.index[entry_idx].date().isoformat(),
                "entry_execution": execution["mode"],
                "signal_close": execution["signal_close"],
                "next_open": execution["next_open"],
                "signal_to_open_return_pct": execution["signal_to_open_return_pct"],
                "signal_to_fill_return_pct": execution["signal_to_fill_return_pct"],
                "exit_start_idx": entry_idx if execution["allow_entry_day_exit"] else entry_idx + 1,
                "signal_score": (
                    abs(float(features["SHOCK"].iloc[max(0, idx - 3):idx].min()))
                    if signal_side == "LONG"
                    else -abs(float(features["SHOCK"].iloc[max(0, idx - 3):idx].max()))
                ),
                "atr": atr,
                "stop_price": stop_price,
                "remaining": 1.0,
                "realized": 0.0,
                "partial_taken": False,
                "best_price": entry_price,
                "exit_events": [],
            }
            continue

        side_name = position["side"]
        if idx < position["exit_start_idx"]:
            continue
        if _stop_hit(side_name, day, position["stop_price"]):
            fill = _stop_fill(side_name, float(day["Open"]), position["stop_price"])
            add_exit(position, idx, "ATR stop", position["remaining"], fill)
            finish_trade(position)
            position = None
            continue

        if side_name == "LONG":
            position["best_price"] = max(position["best_price"], float(day["High"]))
            partial_reached = float(day["SHOCK"]) >= -REVERSION_PARTIAL_LEVEL
            mean_reached = float(day["SHOCK"]) >= 0.0
            progressed = position["best_price"] >= position["entry_price"] + 0.5 * position["atr"]
        else:
            position["best_price"] = min(position["best_price"], float(day["Low"]))
            partial_reached = float(day["SHOCK"]) <= REVERSION_PARTIAL_LEVEL
            mean_reached = float(day["SHOCK"]) <= 0.0
            progressed = position["best_price"] <= position["entry_price"] - 0.5 * position["atr"]

        held_days = idx - position["entry_idx"] + 1
        if mean_reached:
            position["pending_exit"] = {"reason": "mean reversion", "final": True}
        elif not position["partial_taken"] and partial_reached:
            position["pending_exit"] = {"reason": "partial at ±0.5σ", "final": False}
            position["partial_taken"] = True
        elif held_days >= REVERSION_NO_PROGRESS_DAYS and not progressed:
            position["pending_exit"] = {"reason": "3d no progress", "final": True}
        elif held_days >= REVERSION_MAX_HOLDING_DAYS:
            position["pending_exit"] = {"reason": "10d time exit", "final": True}

    if position is not None and position["remaining"] > 1e-12:
        final_idx = len(features) - 1
        add_exit(
            position,
            final_idx,
            "evaluation end mark",
            position["remaining"],
            float(features["Close"].iloc[final_idx]),
        )
        finish_trade(position)

    if not trades:
        result = (0.0, 0.0, 0, period_label, [])
        return result if include_trades else result[:4]
    avg_return = sum(trades) / len(trades)
    win_rate = sum(value > 0 for value in trades) / len(trades) * 100.0
    result = (avg_return, win_rate, len(trades), period_label, trade_details)
    return result if include_trades else result[:4]


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


def scan_value_reversion(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    progress_callback=None,
    report_artifacts=None,
    brain_alpha_policy=BRAIN_ALPHA_OFF,
):
    brain_alpha_policy = normalize_brain_alpha_policy(brain_alpha_policy)
    brain_alpha_overlay = build_brain_alpha_trade_overlay(brain_alpha_policy)
    scored, failed = [], []
    raw_scores = {}
    total_tickers = len(universe_list)
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            result = analyze_value_reversion(
                ticker, allow_fundamental_download=False
            )
            succeeded = result["status"] not in ("DATA_INSUFFICIENT", "ERROR")
            if result["status"] == "OK":
                scored.append({"ticker": ticker, **result})
                raw_scores[ticker] = result["score"]
            elif result["status"] == "DATA_INSUFFICIENT":
                failed.append(ticker)
            else:
                raw_scores[ticker] = 0.0
        finally:
            _notify_progress(
                progress_callback,
                "CURRENT_SCAN",
                completed,
                total_tickers,
                ticker,
                succeeded,
            )

    if brain_alpha_overlay is not None:
        scored = brain_alpha_overlay(scored, universe_list)
        selected_tickers = {row["ticker"] for row in scored}
        for ticker in raw_scores:
            if raw_scores[ticker] and ticker not in selected_tickers:
                raw_scores[ticker] = 0.0

    neutralized_scores = core_cross_sectional_neutralization.neutralize_scores(
        raw_scores, neutralization
    )
    neutralization = core_cross_sectional_neutralization.normalize_mode(neutralization)
    position_mode = core_position_mode.normalize_mode(position_mode)
    for row in scored:
        row["neutralized_score"] = neutralized_scores.get(row["ticker"], row["score"])
    eligible = [
        row
        for row in scored
        if (
            row["side"] == "LONG"
            and core_position_mode.allows_long(position_mode)
        )
        or (
            row["side"] == "SHORT"
            and core_position_mode.allows_short(position_mode)
        )
    ]
    candidate_pairs = [
        (
            Signal(
                strategy="VALUE_REVERSION",
                ticker=row["ticker"],
                side=row["side"],
                signal_date="LATEST_CLOSE",
                raw_score=row["score"],
                status=row["status"],
                metadata={
                    key: row[key]
                    for key in ("brain_alpha_policy", "brain_alpha_score")
                    if key in row
                },
                entry_execution="NEXT_OPEN",
            ),
            row["neutralized_score"],
        )
        for row in eligible
    ]
    scored_by_ticker = {
        item.signal.ticker: item
        for item in core_signal_scoring.score_signal_cohort(candidate_pairs)
    }
    eligible = [
        {
            **row,
            "rank_score": scored_by_ticker[row["ticker"]].rank_score,
            "requested_weight": scored_by_ticker[row["ticker"]].requested_weight,
        }
        for row in eligible
        if row["ticker"] in scored_by_ticker
    ]
    top_longs = sorted(
        (row for row in eligible if row["side"] == "LONG"),
        key=lambda row: (row["rank_score"], row["score"]),
        reverse=True,
    )[:3]
    top_shorts = sorted(
        (row for row in eligible if row["side"] == "SHORT"),
        key=lambda row: (row["rank_score"], row["score"]),
    )[:3]
    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_reversion_backtest,
        period_days=1060,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        signal_score_overlay=brain_alpha_overlay,
        progress_callback=_stage_progress_callback(
            progress_callback, "IS_BACKTEST"
        ),
        side=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.selection_window(),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_reversion_backtest,
        period_days=1060,
        neutralization=neutralization,
        position_mode=position_mode,
        portfolio_accounting=True,
        signal_score_overlay=brain_alpha_overlay,
        progress_callback=_stage_progress_callback(
            progress_callback, "OOS_BACKTEST"
        ),
        side=core_position_mode.side_filter(position_mode),
        **core_backtest_periods.oos_window(),
    )
    msg = (
        "**Reversion Scan Report**\n"
        "=============================\n\n"
        f"{core_cross_sectional_neutralization.report_line(neutralization, raw_scores)}\n"
        f"{core_position_mode.report_line(position_mode)}\n"
        "Scope: today's candidates and entry-date IS/OOS cross-sections; current metadata labels.\n\n"
        "**LONG Candidates**\n"
        "Rule: 5d downside shock <= -1.40σ within 3d, near-flat/up MA60, positive rebound close, turnover <2.0x.\n"
        f"{_format_rows(top_longs, long_side=True)}\n"
        "**SHORT Candidates**\n"
        "Rule: 5d upside shock >= +1.60σ within 3d, near-flat/down MA60, reversal close, turnover <2.0x.\n"
        f"{_format_rows(top_shorts, long_side=False)}"
        f"{core_universe_backtest.format_fixed_is_oos_backtest(selection_result, oos_result, entry_text='shock within prior 3d, near-flat/trend-aligned MA60, close-confirmed reversal, positive LONG candle, and turnover <2.0x; enter next open', exit_text='50% at ±0.5σ, move residual stop to entry, then exit at mean; ATR stop, 3d no-progress, or 10d time exit', strategy_key='price_liquidity_reversion', report_artifacts=report_artifacts)}"
    )
    if failed:
        msg += "\n----------------------------------------\nInsufficient local data: " + ", ".join(sorted(failed))
    return msg
