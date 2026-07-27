"""Experimental portfolio-aware capital-redistribution policy.

The policy never creates a signal, changes its direction, or changes an exit.
It redistributes each side's close-known next-open cohort capital among the
existing candidates while preserving the side's requested gross. Training
uses exact Portfolio Beta ledger replays and an ACWI-relative objective.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

import core_portfolio_performance


EXPERIMENTAL = True
FEATURE_SCHEMA_VERSION = "portfolio-rl-overlay-1.0"
POLICY_SCHEMA_VERSION = "portfolio-rl-redistribution-2.0"
ACTION_MODE = "SIDE_GROSS_PRESERVING_REDISTRIBUTION"
DEFAULT_SEEDS = (11, 23, 37, 53, 71)
MAX_NAME_WEIGHT = 0.05
MAX_ABS_LOG_TILT = 2.0
MINIMUM_CLOSE_HISTORY = 66

STATIC_FEATURE_NAMES = (
    "direction",
    "directional_return_5d_pct",
    "directional_return_20d_pct",
    "directional_return_60d_pct",
    "volatility_20d_pct",
    "atr_14_pct",
    "directional_ma20_distance_pct",
    "directional_ma60_slope_pct",
    "turnover_ratio_20d",
    "directional_gap_mean_20d_pct",
    "gap_std_20d_pct",
    "rank_strength",
    "base_weight_fraction",
    "lagged_acwi_return_20d_pct",
    "lagged_acwi_volatility_20d_pct",
    "lagged_acwi_correlation_60d",
    "structure_flat",
    "structure_trend",
    "strategy_momentum",
    "strategy_box",
    "strategy_reversion",
)

def _finite_float(value, name):
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    return converted


def _unit_interval(value, name):
    converted = _finite_float(value, name)
    if not 0.0 <= converted <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return converted


def _positive_int(value, name):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be a positive integer")
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if converted <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return converted


def _normalize_frame(frame):
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index).normalize()
    result = result[~result.index.duplicated(keep="last")].sort_index()
    return result


def _close_series(frame, *, adjusted=False):
    frame = _normalize_frame(frame)
    columns = ("Adj Close", "Close") if adjusted else ("Close",)
    for column in columns:
        if column not in frame:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        values = values.replace([np.inf, -np.inf], np.nan).dropna()
        values = values[values > 0]
        if not values.empty:
            return values
    return pd.Series(dtype=float)


def _signal_date(trade, frame):
    entry_date = pd.Timestamp(trade["entry_date"]).normalize()
    supplied = trade.get("signal_date")
    if supplied is not None and str(supplied).strip():
        signal_date = pd.Timestamp(supplied).normalize()
        if signal_date >= entry_date:
            raise ValueError("signal_date must be earlier than entry_date")
        return signal_date

    frame = _normalize_frame(frame)
    earlier = frame.index[frame.index < entry_date]
    if earlier.empty:
        raise ValueError("cannot infer a completed signal bar before entry_date")
    return pd.Timestamp(earlier[-1]).normalize()


def _generic_close_features(frame, signal_date, side):
    frame = _normalize_frame(frame)
    required = ("Open", "High", "Low", "Close", "Volume")
    if any(column not in frame for column in required):
        raise ValueError("ticker OHLCV is missing a required column")
    history = frame.loc[:signal_date, required].dropna()
    if len(history) < MINIMUM_CLOSE_HISTORY:
        raise ValueError("insufficient close-known ticker history")

    close = history["Close"].astype(float)
    high = history["High"].astype(float)
    low = history["Low"].astype(float)
    volume = history["Volume"].astype(float)
    current_close = float(close.iloc[-1])
    if current_close <= 0:
        raise ValueError("signal close must be positive")
    direction = 1.0 if str(side).upper() == "LONG" else -1.0

    def directional_return(days):
        previous = float(close.iloc[-days - 1])
        if previous <= 0:
            raise ValueError("historical close must be positive")
        return direction * (current_close / previous - 1.0) * 100.0

    daily_returns = close.pct_change()
    volatility_20 = float(daily_returns.tail(20).std(ddof=0) * 100.0)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_14_pct = float(true_range.tail(14).mean() / current_close * 100.0)
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    prior_ma60 = float(close.iloc[-65:-5].mean())
    ma20_distance = direction * (current_close / ma20 - 1.0) * 100.0
    ma60_slope = direction * (ma60 / prior_ma60 - 1.0) * 100.0
    turnover = volume * close
    prior_turnover = float(turnover.iloc[-21:-1].mean())
    turnover_ratio = (
        float(turnover.iloc[-1] / prior_turnover)
        if prior_turnover > 0
        else 0.0
    )
    gaps = (history["Open"].astype(float) / close.shift(1) - 1.0).tail(20)
    gap_mean = direction * float(gaps.mean() * 100.0)
    gap_std = float(gaps.std(ddof=0) * 100.0)
    values = (
        direction,
        directional_return(5),
        directional_return(20),
        directional_return(60),
        volatility_20,
        atr_14_pct,
        ma20_distance,
        ma60_slope,
        turnover_ratio,
        gap_mean,
        gap_std,
    )
    if not np.isfinite(values).all():
        raise ValueError("ticker close-known features are not finite")
    return values


def _lagged_market_features(frame, market_frame, signal_date):
    ticker_close = _close_series(frame)
    # Strictly earlier ACWI closes avoid same-calendar-day leakage for JP/CH.
    market_close = _close_series(market_frame, adjusted=True)
    market_close = market_close[market_close.index < signal_date]
    if len(market_close) < 61:
        raise ValueError("insufficient strictly lagged ACWI history")
    market_return = (
        float(market_close.iloc[-1] / market_close.iloc[-21] - 1.0) * 100.0
    )
    market_returns = market_close.pct_change().dropna()
    market_volatility = (
        float(market_returns.tail(20).std(ddof=0)) * math.sqrt(252.0) * 100.0
    )

    ticker_returns = ticker_close.loc[:signal_date].pct_change().dropna()
    aligned = pd.concat(
        [
            ticker_returns.rename("ticker"),
            market_returns.rename("market"),
        ],
        axis=1,
        join="inner",
    ).dropna().tail(60)
    correlation = (
        float(aligned["ticker"].corr(aligned["market"]))
        if len(aligned) >= 20
        else 0.0
    )
    values = (market_return, market_volatility, correlation)
    if not np.isfinite(values).all():
        raise ValueError("lagged ACWI features are not finite")
    return values


def _strategy_flags(strategy):
    normalized = str(strategy or "").strip().upper().replace("_", " ")
    momentum = float("MOMENTUM" in normalized or normalized == "MA")
    box = float("BOX" in normalized)
    reversion = float("REVERSION" in normalized)
    return momentum, box, reversion


def _raw_feature_vector(trade, frame, market_frame):
    side = str(trade.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("trade side must be LONG or SHORT")
    signal_date = _signal_date(trade, frame)
    entry_date = pd.Timestamp(trade["entry_date"]).normalize()
    if signal_date >= entry_date:
        raise ValueError("point-in-time feature date must precede entry")

    generic = _generic_close_features(frame, signal_date, side)
    market = _lagged_market_features(frame, market_frame, signal_date)
    score = trade.get("rank_score", trade.get("percentile_rank"))
    if score is None:
        score = trade.get("selected_score", trade.get("signal_score", 0.0))
    rank_strength = min(1.0, abs(_finite_float(score, "rank_strength")))
    requested_weight = _finite_float(
        trade.get("requested_weight", 0.0), "requested_weight"
    )
    base_weight_fraction = min(
        1.0, max(0.0, requested_weight / MAX_NAME_WEIGHT)
    )
    structure = str(trade.get("structure") or "").strip().upper()
    flags = (
        float(structure == "FLAT"),
        float(structure in ("RISING", "FALLING", "TREND")),
        *_strategy_flags(trade.get("strategy")),
    )
    values = (*generic, rank_strength, base_weight_fraction, *market, *flags)
    if len(values) != len(STATIC_FEATURE_NAMES):
        raise AssertionError("policy feature schema length mismatch")
    if not np.isfinite(values).all():
        raise ValueError("policy features are not finite")
    return signal_date, tuple(float(value) for value in values)


@dataclass(frozen=True)
class FeatureScaler:
    """Train-only standardizer with explicit schema metadata."""

    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    @classmethod
    def fit(cls, values, feature_names=STATIC_FEATURE_NAMES):
        matrix = np.asarray(values, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[0] == 0:
            raise ValueError("feature training matrix must be non-empty and 2D")
        if matrix.shape[1] != len(feature_names):
            raise ValueError("feature training matrix has the wrong width")
        if not np.isfinite(matrix).all():
            raise ValueError("feature training matrix must be finite")
        means = matrix.mean(axis=0)
        scales = matrix.std(axis=0, ddof=0)
        scales = np.where(scales <= 1e-12, 1.0, scales)
        return cls(
            tuple(feature_names),
            tuple(float(value) for value in means),
            tuple(float(value) for value in scales),
        )

    def transform(self, values):
        vector = np.asarray(values, dtype=np.float64)
        if vector.shape != (len(self.feature_names),):
            raise ValueError("feature vector has the wrong width")
        if not np.isfinite(vector).all():
            raise ValueError("feature vector must be finite")
        return (vector - np.asarray(self.means)) / np.asarray(self.scales)

    def to_dict(self):
        return {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "feature_names": list(self.feature_names),
            "means": list(self.means),
            "scales": list(self.scales),
        }


@dataclass(frozen=True)
class FeaturePreparation:
    trades: tuple[dict, ...]
    scaler: FeatureScaler
    skipped: tuple[dict, ...]


def prepare_point_in_time_trades(
    trades,
    price_data,
    market_frame,
    *,
    scaler=None,
):
    """Attach signal-close features and fail closed on unsafe observations."""

    raw_rows = []
    passthrough_rows = []
    skipped = []
    for sequence, raw_trade in enumerate(trades):
        trade = dict(raw_trade)
        ticker = str(trade.get("ticker") or "").upper()
        observation_id = str(
            trade.get("trade_id") or f"OBS{sequence + 1:06d}"
        )
        trade["policy_observation_id"] = observation_id
        frame = price_data.get(ticker)
        try:
            signal_date, features = _raw_feature_vector(
                trade, frame, market_frame
            )
            trade["ticker"] = ticker
            trade["signal_date"] = signal_date.date().isoformat()
            trade["feature_asof_date"] = signal_date.date().isoformat()
            raw_rows.append((sequence, trade, features))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            trade["ticker"] = ticker
            trade["policy_feature_eligible"] = False
            trade["policy_feature_skip_reason"] = (
                f"{type(exc).__name__}: {exc}"
            )
            passthrough_rows.append((sequence, trade))
            skipped.append(
                {
                    "trade_id": str(trade.get("trade_id") or ""),
                    "policy_observation_id": observation_id,
                    "ticker": ticker,
                    "entry_date": str(trade.get("entry_date") or ""),
                    "reason": trade["policy_feature_skip_reason"],
                }
            )

    if not raw_rows and scaler is None:
        raise ValueError("no point-in-time-safe policy observations")
    if scaler is None:
        scaler = FeatureScaler.fit(
            [features for _, _, features in raw_rows]
        )
    if not isinstance(scaler, FeatureScaler):
        raise TypeError("scaler must be a FeatureScaler")
    if scaler.feature_names != tuple(STATIC_FEATURE_NAMES):
        raise ValueError("feature scaler schema does not match this experiment")

    prepared = []
    for sequence, trade, features in raw_rows:
        item = dict(trade)
        item["policy_feature_eligible"] = True
        item["policy_feature_schema"] = FEATURE_SCHEMA_VERSION
        item["policy_features"] = tuple(
            float(value) for value in scaler.transform(features)
        )
        prepared.append((sequence, item))
    prepared.extend(
        (sequence, dict(trade))
        for sequence, trade in passthrough_rows
    )
    prepared.sort(
        key=lambda item: item[0]
    )
    return FeaturePreparation(
        tuple(item for _, item in prepared),
        scaler,
        tuple(skipped),
    )


def _capped_redistributed_weights(base_weights, raw_tilts):
    """Tilt base weights while preserving their sum and the name cap."""

    base = np.asarray(base_weights, dtype=np.float64)
    tilts = np.asarray(raw_tilts, dtype=np.float64)
    if base.ndim != 1 or base.shape != tilts.shape or base.size == 0:
        raise ValueError("redistribution inputs must be non-empty 1D peers")
    if not np.isfinite(base).all() or not np.isfinite(tilts).all():
        raise ValueError("redistribution inputs must be finite")
    if (base <= 0.0).any() or (base > MAX_NAME_WEIGHT + 1e-12).any():
        raise ValueError("base weights must be within the positive name cap")

    target_total = float(base.sum())
    clipped = np.clip(tilts, -MAX_ABS_LOG_TILT, MAX_ABS_LOG_TILT)
    preferences = base * np.exp(clipped - float(clipped.max()))
    targets = np.zeros_like(base)
    remaining = list(range(base.size))
    remaining_total = target_total

    while remaining:
        remaining_preferences = preferences[remaining]
        preference_sum = float(remaining_preferences.sum())
        if preference_sum <= 0.0:
            proposed = np.full(
                len(remaining),
                remaining_total / len(remaining),
            )
        else:
            proposed = (
                remaining_preferences
                / preference_sum
                * remaining_total
            )
        capped_positions = [
            position
            for position, weight in enumerate(proposed)
            if weight > MAX_NAME_WEIGHT + 1e-12
        ]
        if not capped_positions:
            for position, index in enumerate(remaining):
                targets[index] = proposed[position]
            remaining_total = 0.0
            break
        capped_indices = {
            remaining[position] for position in capped_positions
        }
        for index in capped_indices:
            targets[index] = MAX_NAME_WEIGHT
        remaining_total -= MAX_NAME_WEIGHT * len(capped_indices)
        remaining = [
            index for index in remaining if index not in capped_indices
        ]

    residual = target_total - float(targets.sum())
    if residual > 1e-12:
        for index in np.argsort(-(MAX_NAME_WEIGHT - targets)):
            room = MAX_NAME_WEIGHT - targets[index]
            addition = min(float(room), residual)
            targets[index] += addition
            residual -= addition
            if residual <= 1e-12:
                break
    elif residual < -1e-12:
        for index in np.argsort(-targets):
            reduction = min(float(targets[index]), -residual)
            targets[index] -= reduction
            residual += reduction
            if residual >= -1e-12:
                break

    if abs(float(targets.sum()) - target_total) > 1e-9:
        raise RuntimeError("redistribution failed to preserve cohort capital")
    return targets


@dataclass(frozen=True)
class ContinuousSizingPolicy:
    """Linear LONG/SHORT tilt policy with side-gross preservation."""

    long_parameters: tuple[float, ...]
    short_parameters: tuple[float, ...]

    @property
    def static_feature_count(self):
        return len(STATIC_FEATURE_NAMES)

    @property
    def size_input_count(self):
        return self.static_feature_count + 4

    def __post_init__(self):
        if len(self.long_parameters) != self.size_input_count:
            raise ValueError("LONG parameter vector has the wrong width")
        if len(self.short_parameters) != self.size_input_count:
            raise ValueError("SHORT parameter vector has the wrong width")
        values = self.to_vector()
        if not np.isfinite(values).all():
            raise ValueError("policy parameters must be finite")

    @classmethod
    def initial(cls):
        size_count = len(STATIC_FEATURE_NAMES) + 4
        return cls(
            tuple(np.zeros(size_count)),
            tuple(np.zeros(size_count)),
        )

    @classmethod
    def from_vector(cls, values):
        vector = np.asarray(values, dtype=np.float64)
        size_count = len(STATIC_FEATURE_NAMES) + 4
        expected = size_count * 2
        if vector.shape != (expected,) or not np.isfinite(vector).all():
            raise ValueError("flat policy vector has the wrong shape or values")
        return cls(
            tuple(vector[:size_count]),
            tuple(vector[size_count:]),
        )

    def to_vector(self):
        return np.asarray(
            (*self.long_parameters, *self.short_parameters),
            dtype=np.float64,
        )

    def decide(self, signals, portfolio_state):
        if not signals:
            return {"gross_scale": 1.0, "trade_multipliers": {}}
        state = dict(portfolio_state or {})
        current_gross = _unit_interval(
            max(0.0, min(1.0, float(state.get("gross_fraction", 0.0)))),
            "gross_fraction",
        )
        current_net = float(
            np.clip(float(state.get("net_fraction", 0.0)), -1.0, 1.0)
        )
        long_fraction = max(
            0.0, min(1.0, float(state.get("long_fraction", 0.0)))
        )
        short_fraction = max(
            0.0, min(1.0, float(state.get("short_fraction", 0.0)))
        )
        active_scaled = min(
            1.0, max(0.0, float(state.get("active_positions", 0))) / 20.0
        )

        eligible_by_side = {"LONG": [], "SHORT": []}
        multipliers = {
            str(signal["trade_id"]): 1.0 for signal in signals
        }
        for signal in signals:
            if signal.get("policy_feature_eligible") is False:
                continue
            features = np.asarray(
                signal.get("policy_features"), dtype=np.float64
            )
            if features.shape != (self.static_feature_count,):
                raise ValueError("sanitized signal has an invalid policy feature")
            if not np.isfinite(features).all():
                raise ValueError("sanitized signal policy features must be finite")
            side = str(signal.get("side") or "").upper()
            if side == "LONG":
                side_fraction = long_fraction
                parameters = np.asarray(self.long_parameters)
            elif side == "SHORT":
                side_fraction = short_fraction
                parameters = np.asarray(self.short_parameters)
            else:
                raise ValueError("sanitized signal side must be LONG or SHORT")
            base_weight = _finite_float(
                signal.get("requested_weight", 0.0),
                "requested_weight",
            )
            if base_weight <= 0.0:
                continue
            size_input = np.asarray(
                (
                    *features,
                    current_gross,
                    current_net,
                    side_fraction,
                    active_scaled,
                )
            )
            eligible_by_side[side].append(
                (
                    signal,
                    base_weight,
                    float(np.dot(parameters, size_input)),
                )
            )

        for items in eligible_by_side.values():
            if not items:
                continue
            targets = _capped_redistributed_weights(
                [item[1] for item in items],
                [item[2] for item in items],
            )
            for (signal, base_weight, _), target_weight in zip(
                items, targets
            ):
                multipliers[str(signal["trade_id"])] = (
                    float(target_weight) / base_weight
                )
        return {
            "gross_scale": 1.0,
            "trade_multipliers": multipliers,
        }

    def entry_sizing_policy(self, decision_log=None):
        def resolve(*, signals, portfolio_state, **context):
            decision = self.decide(signals, portfolio_state)
            if decision_log is not None:
                for signal in signals:
                    trade_id = str(signal["trade_id"])
                    decision_log.append(
                        {
                            "entry_date": str(
                                signal.get("entry_date")
                                or context.get("entry_date")
                                or ""
                            ),
                            "decision_asof_date": str(
                                portfolio_state.get("asof_date") or ""
                            ),
                            "trade_id": trade_id,
                            "ticker": signal.get("ticker"),
                            "side": signal.get("side"),
                            "gross_scale": decision["gross_scale"],
                            "trade_multiplier": decision[
                                "trade_multipliers"
                            ][trade_id],
                        }
                    )
            return decision

        return resolve

    def to_dict(self):
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "action_mode": ACTION_MODE,
            "max_abs_log_tilt": MAX_ABS_LOG_TILT,
            "long_parameters": list(self.long_parameters),
            "short_parameters": list(self.short_parameters),
        }


@dataclass(frozen=True)
class EnsembleSizingPolicy:
    policies: tuple[ContinuousSizingPolicy, ...]

    def __post_init__(self):
        if not self.policies:
            raise ValueError("ensemble must contain at least one policy")

    def decide(self, signals, portfolio_state):
        decisions = [
            policy.decide(signals, portfolio_state)
            for policy in self.policies
        ]
        trade_ids = {str(signal["trade_id"]) for signal in signals}
        return {
            "gross_scale": float(
                np.mean([item["gross_scale"] for item in decisions])
            ),
            "trade_multipliers": {
                trade_id: float(
                    np.mean(
                        [
                            item["trade_multipliers"][trade_id]
                            for item in decisions
                        ]
                    )
                )
                for trade_id in trade_ids
            },
        }

    def entry_sizing_policy(self, decision_log=None):
        def resolve(*, signals, portfolio_state, **context):
            decision = self.decide(signals, portfolio_state)
            if decision_log is not None:
                for signal in signals:
                    trade_id = str(signal["trade_id"])
                    decision_log.append(
                        {
                            "entry_date": str(
                                signal.get("entry_date")
                                or context.get("entry_date")
                                or ""
                            ),
                            "decision_asof_date": str(
                                portfolio_state.get("asof_date") or ""
                            ),
                            "trade_id": trade_id,
                            "ticker": signal.get("ticker"),
                            "side": signal.get("side"),
                            "gross_scale": decision["gross_scale"],
                            "trade_multiplier": decision[
                                "trade_multipliers"
                            ][trade_id],
                        }
                    )
            return decision

        return resolve


def downside_cvar(daily_returns, confidence=0.95):
    confidence = _unit_interval(confidence, "confidence")
    if confidence <= 0.5 or confidence >= 1.0:
        raise ValueError("confidence must be strictly between 0.5 and 1")
    returns = np.asarray(daily_returns, dtype=np.float64)
    returns = returns[np.isfinite(returns)]
    if returns.size == 0:
        raise ValueError("daily returns are required for CVaR")
    raw_tail_count = (1.0 - confidence) * returns.size
    tail_count = max(1, int(math.ceil(raw_tail_count - 1e-12)))
    worst = np.sort(returns)[:tail_count]
    return max(0.0, -float(np.mean(worst)))


def policy_metrics(performance, confidence=0.95):
    if not performance.get("market_benchmark_daily_curve_available"):
        raise ValueError("complete daily ACWI curve is required for RL reward")
    daily = performance["daily"]
    strategy_returns = pd.to_numeric(
        daily["strategy_return"], errors="coerce"
    )
    market_returns = pd.to_numeric(
        daily["market_benchmark_return"], errors="coerce"
    )
    if strategy_returns.isna().any() or market_returns.isna().any():
        raise ValueError("RL reward cannot use an incomplete daily return curve")
    if (strategy_returns <= -1.0).any() or (market_returns <= -1.0).any():
        raise ValueError("daily returns must be greater than -100%")
    excess_log_return = float(
        (np.log1p(strategy_returns) - np.log1p(market_returns)).sum()
    )
    strategy = performance["strategy"]
    market = performance["market_benchmark"]
    ledger_trades = list(performance["ledger"].get("trades", []))
    filled = [
        trade
        for trade in ledger_trades
        if trade.get("allocation_status") == "FILLED"
    ]

    def trade_statistics(side=None):
        selected = [
            trade
            for trade in filled
            if side is None
            or str(trade.get("side") or "").upper() == side
        ]
        returns = [
            float(trade["evaluated_return_pct"])
            for trade in selected
            if trade.get("evaluated_return_pct") is not None
            and math.isfinite(float(trade["evaluated_return_pct"]))
        ]
        entry_weights = [
            float(trade["entry_weight_pct"])
            for trade in selected
            if trade.get("entry_weight_pct") is not None
            and math.isfinite(float(trade["entry_weight_pct"]))
        ]
        return {
            "count": len(selected),
            "average_return_pct": (
                float(np.mean(returns)) if returns else None
            ),
            "win_rate_pct": (
                sum(value > 0.0 for value in returns) / len(returns) * 100.0
                if returns
                else None
            ),
            "average_entry_weight_pct": (
                float(np.mean(entry_weights)) if entry_weights else None
            ),
        }

    all_trades = trade_statistics()
    long_trades = trade_statistics("LONG")
    short_trades = trade_statistics("SHORT")
    feature_eligible_candidates = [
        trade
        for trade in ledger_trades
        if trade.get("policy_feature_eligible") is not False
        and trade.get("policy_features") is not None
    ]
    feature_eligible_filled = [
        trade
        for trade in feature_eligible_candidates
        if trade.get("allocation_status") == "FILLED"
    ]
    passthrough_candidates = [
        trade
        for trade in ledger_trades
        if trade.get("policy_feature_eligible") is False
    ]
    passthrough_filled = [
        trade
        for trade in passthrough_candidates
        if trade.get("allocation_status") == "FILLED"
    ]

    def average_annotation(trades, field, default):
        if not trades:
            return None
        return float(
            np.mean(
                [
                    float(trade.get(field, default))
                    for trade in trades
                ]
            )
        )

    eligible_candidate_effective_scales = [
        float(trade.get("policy_gross_scale", 1.0))
        * float(trade.get("policy_trade_multiplier", 1.0))
        for trade in feature_eligible_candidates
    ]

    def redistribution_statistics(trades):
        base_weights = np.asarray(
            [
                float(trade.get("base_requested_weight", 0.0))
                for trade in trades
            ],
            dtype=np.float64,
        )
        requested_weights = np.asarray(
            [
                float(trade.get("requested_weight", 0.0))
                for trade in trades
            ],
            dtype=np.float64,
        )
        positive = base_weights > 0.0
        base_weights = base_weights[positive]
        requested_weights = requested_weights[positive]
        base_total = float(base_weights.sum())
        if base_total <= 0.0:
            return {
                "capital_preservation_ratio": None,
                "redistribution_turnover_pct": None,
            }
        return {
            "capital_preservation_ratio": (
                float(requested_weights.sum()) / base_total
            ),
            "redistribution_turnover_pct": (
                0.5
                * float(np.abs(requested_weights - base_weights).sum())
                / base_total
                * 100.0
            ),
        }

    eligible_redistribution = redistribution_statistics(
        feature_eligible_candidates
    )

    def eligible_side(side):
        candidates = [
            trade
            for trade in feature_eligible_candidates
            if str(trade.get("side") or "").upper() == side
        ]
        filled_side = [
            trade
            for trade in candidates
            if trade.get("allocation_status") == "FILLED"
        ]
        scales = [
            float(trade.get("policy_gross_scale", 1.0))
            * float(trade.get("policy_trade_multiplier", 1.0))
            for trade in candidates
        ]
        return {
            "candidates": len(candidates),
            "filled": len(filled_side),
            "average_effective_scale": (
                float(np.mean(scales)) if scales else None
            ),
            **redistribution_statistics(candidates),
        }

    eligible_long = eligible_side("LONG")
    eligible_short = eligible_side("SHORT")
    return {
        "excess_log_return": excess_log_return,
        "cvar": downside_cvar(strategy_returns, confidence=confidence),
        "roi_pct": strategy["roi_pct"],
        "cagr_pct": strategy["cagr_pct"],
        "mdd_pct": strategy["mdd_pct"],
        "sharpe": strategy["sharpe"],
        "acwi_roi_pct": market["roi_pct"],
        "filled_trades": performance["ledger"]["filled_trades"],
        "skipped_trades": performance["ledger"]["skipped_trades"],
        "average_gross_pct": float(daily["gross_exposure_pct"].mean()),
        "maximum_gross_pct": float(daily["gross_exposure_pct"].max()),
        "trade_win_rate_pct": all_trades["win_rate_pct"],
        "average_trade_return_pct": all_trades["average_return_pct"],
        "long_filled_trades": long_trades["count"],
        "long_win_rate_pct": long_trades["win_rate_pct"],
        "long_average_trade_return_pct": long_trades[
            "average_return_pct"
        ],
        "long_average_entry_weight_pct": long_trades[
            "average_entry_weight_pct"
        ],
        "short_filled_trades": short_trades["count"],
        "short_win_rate_pct": short_trades["win_rate_pct"],
        "short_average_trade_return_pct": short_trades[
            "average_return_pct"
        ],
        "short_average_entry_weight_pct": short_trades[
            "average_entry_weight_pct"
        ],
        "feature_eligible_candidates": len(feature_eligible_candidates),
        "feature_eligible_filled_trades": len(feature_eligible_filled),
        "passthrough_candidates": len(passthrough_candidates),
        "passthrough_filled_trades": len(passthrough_filled),
        "average_policy_gross_scale": average_annotation(
            filled,
            "policy_gross_scale",
            1.0,
        ),
        "average_policy_trade_multiplier": average_annotation(
            filled,
            "policy_trade_multiplier",
            1.0,
        ),
        "eligible_average_policy_gross_scale": average_annotation(
            feature_eligible_candidates,
            "policy_gross_scale",
            1.0,
        ),
        "eligible_average_policy_trade_multiplier": average_annotation(
            feature_eligible_candidates,
            "policy_trade_multiplier",
            1.0,
        ),
        "eligible_average_effective_scale": (
            float(np.mean(eligible_candidate_effective_scales))
            if eligible_candidate_effective_scales
            else None
        ),
        "eligible_capital_preservation_ratio": (
            eligible_redistribution["capital_preservation_ratio"]
        ),
        "eligible_redistribution_turnover_pct": (
            eligible_redistribution["redistribution_turnover_pct"]
        ),
        "eligible_long_candidates": eligible_long["candidates"],
        "eligible_long_filled_trades": eligible_long["filled"],
        "eligible_long_average_effective_scale": eligible_long[
            "average_effective_scale"
        ],
        "eligible_long_capital_preservation_ratio": eligible_long[
            "capital_preservation_ratio"
        ],
        "eligible_long_redistribution_turnover_pct": eligible_long[
            "redistribution_turnover_pct"
        ],
        "eligible_short_candidates": eligible_short["candidates"],
        "eligible_short_filled_trades": eligible_short["filled"],
        "eligible_short_average_effective_scale": eligible_short[
            "average_effective_scale"
        ],
        "eligible_short_capital_preservation_ratio": eligible_short[
            "capital_preservation_ratio"
        ],
        "eligible_short_redistribution_turnover_pct": eligible_short[
            "redistribution_turnover_pct"
        ],
        "average_net_exposure_pct": float(
            daily.get(
                "net_exposure_pct",
                pd.Series(0.0, index=daily.index),
            ).mean()
        ),
    }


def constrained_objective(metrics, cvar_limit, dual_multiplier, l2_cost=0.0):
    limit = max(0.0, _finite_float(cvar_limit, "cvar_limit"))
    dual = max(
        0.0, _finite_float(dual_multiplier, "dual_multiplier")
    )
    violation = max(0.0, float(metrics["cvar"]) - limit)
    return (
        float(metrics["excess_log_return"])
        - dual * violation
        - max(0.0, float(l2_cost))
    )


def _policy_l2_cost(parameters, penalty):
    """Regularize every parameter relative to the baseline-size policy."""

    penalty = max(0.0, _finite_float(penalty, "l2_penalty"))
    vector = np.asarray(parameters, dtype=np.float64)
    reference = ContinuousSizingPolicy.initial().to_vector()
    difference = vector - reference
    return penalty * float(np.dot(difference, difference))


@dataclass(frozen=True)
class EvolutionStrategyConfig:
    iterations: int = 16
    population: int = 4
    noise_std: float = 0.12
    learning_rate: float = 0.03
    l2_penalty: float = 1e-4
    cvar_confidence: float = 0.95
    cvar_tolerance: float = 1.0
    dual_learning_rate: float = 2.0
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    adam_epsilon: float = 1e-8
    parameter_clip: float = 8.0
    minimum_filled_trades: int = 60
    minimum_feature_eligible_filled_trades: int = 60
    minimum_feature_eligible_fill_ratio: float = 0.50
    minimum_side_feature_eligible_candidates: int = 10
    minimum_average_gross_ratio: float = 0.25
    minimum_eligible_effective_scale: float = 0.25

    def __post_init__(self):
        for name in (
            "iterations",
            "population",
            "minimum_filled_trades",
            "minimum_feature_eligible_filled_trades",
            "minimum_side_feature_eligible_candidates",
        ):
            object.__setattr__(
                self, name, _positive_int(getattr(self, name), name)
            )
        for name in (
            "noise_std",
            "learning_rate",
            "dual_learning_rate",
            "adam_epsilon",
            "parameter_clip",
        ):
            value = _finite_float(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        l2_penalty = _finite_float(self.l2_penalty, "l2_penalty")
        if l2_penalty < 0:
            raise ValueError("l2_penalty must be non-negative")
        object.__setattr__(self, "l2_penalty", l2_penalty)
        confidence = _unit_interval(
            self.cvar_confidence, "cvar_confidence"
        )
        if not 0.5 < confidence < 1.0:
            raise ValueError("cvar_confidence must be between 0.5 and 1")
        object.__setattr__(self, "cvar_confidence", confidence)
        tolerance = _finite_float(self.cvar_tolerance, "cvar_tolerance")
        if tolerance <= 0:
            raise ValueError("cvar_tolerance must be positive")
        object.__setattr__(self, "cvar_tolerance", tolerance)
        for name in ("adam_beta1", "adam_beta2"):
            value = _unit_interval(getattr(self, name), name)
            if not 0 < value < 1:
                raise ValueError(f"{name} must be strictly between 0 and 1")
            object.__setattr__(self, name, value)
        minimum_gross_ratio = _unit_interval(
            self.minimum_average_gross_ratio,
            "minimum_average_gross_ratio",
        )
        if minimum_gross_ratio <= 0:
            raise ValueError(
                "minimum_average_gross_ratio must be greater than 0"
            )
        object.__setattr__(
            self,
            "minimum_average_gross_ratio",
            minimum_gross_ratio,
        )
        minimum_eligible_scale = _unit_interval(
            self.minimum_eligible_effective_scale,
            "minimum_eligible_effective_scale",
        )
        if minimum_eligible_scale <= 0:
            raise ValueError(
                "minimum_eligible_effective_scale must be greater than 0"
            )
        object.__setattr__(
            self,
            "minimum_eligible_effective_scale",
            minimum_eligible_scale,
        )
        minimum_fill_ratio = _unit_interval(
            self.minimum_feature_eligible_fill_ratio,
            "minimum_feature_eligible_fill_ratio",
        )
        if minimum_fill_ratio <= 0:
            raise ValueError(
                "minimum_feature_eligible_fill_ratio must be greater than 0"
            )
        object.__setattr__(
            self,
            "minimum_feature_eligible_fill_ratio",
            minimum_fill_ratio,
        )


def policy_feasibility(metrics, baseline_metrics, config):
    """Apply the same seed and ensemble risk/collapse gates."""

    if not isinstance(config, EvolutionStrategyConfig):
        raise TypeError("config must be an EvolutionStrategyConfig")
    cvar_limit = (
        float(baseline_metrics["cvar"]) * config.cvar_tolerance
    )
    minimum_average_gross_pct = (
        float(baseline_metrics["average_gross_pct"])
        * config.minimum_average_gross_ratio
    )
    minimum_eligible_filled = max(
        config.minimum_feature_eligible_filled_trades,
        int(
            math.ceil(
                float(
                    baseline_metrics[
                        "feature_eligible_filled_trades"
                    ]
                )
                * config.minimum_feature_eligible_fill_ratio
            )
        ),
    )
    checks = {
        "minimum_total_filled": (
            int(metrics["filled_trades"])
            >= config.minimum_filled_trades
        ),
        "cvar": float(metrics["cvar"]) <= cvar_limit + 1e-12,
        "minimum_average_gross": (
            float(metrics["average_gross_pct"])
            >= minimum_average_gross_pct - 1e-12
        ),
        "minimum_feature_eligible_filled": (
            int(metrics["feature_eligible_filled_trades"])
            >= minimum_eligible_filled
        ),
        "minimum_eligible_effective_scale": (
            metrics["eligible_average_effective_scale"] is not None
            and float(metrics["eligible_average_effective_scale"])
            >= config.minimum_eligible_effective_scale - 1e-12
        ),
        "capital_preservation": (
            metrics["eligible_capital_preservation_ratio"] is not None
            and abs(
                float(metrics["eligible_capital_preservation_ratio"]) - 1.0
            )
            <= 1e-8
        ),
    }
    side_limits = {}
    for side in ("long", "short"):
        baseline_candidates = int(
            baseline_metrics[f"eligible_{side}_candidates"]
        )
        if (
            baseline_candidates
            < config.minimum_side_feature_eligible_candidates
        ):
            continue
        minimum_side_filled = int(
            math.ceil(
                float(
                    baseline_metrics[
                        f"eligible_{side}_filled_trades"
                    ]
                )
                * config.minimum_feature_eligible_fill_ratio
            )
        )
        checks[f"{side}_fill_retention"] = (
            int(metrics[f"eligible_{side}_filled_trades"])
            >= minimum_side_filled
        )
        side_scale = metrics[
            f"eligible_{side}_average_effective_scale"
        ]
        checks[f"{side}_effective_scale"] = (
            side_scale is not None
            and float(side_scale)
            >= config.minimum_eligible_effective_scale - 1e-12
        )
        side_limits[side] = {
            "baseline_candidates": baseline_candidates,
            "minimum_filled": minimum_side_filled,
        }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "satisfied": not failed,
        "failed_checks": failed,
        "checks": checks,
        "cvar_limit": cvar_limit,
        "minimum_average_gross_pct": minimum_average_gross_pct,
        "minimum_feature_eligible_filled_trades": (
            minimum_eligible_filled
        ),
        "minimum_eligible_effective_scale": (
            config.minimum_eligible_effective_scale
        ),
        "side_limits": side_limits,
    }


@dataclass(frozen=True)
class TrainingResult:
    policy: ContinuousSizingPolicy
    baseline_metrics: Mapping[str, float]
    trained_metrics: Mapping[str, float]
    cvar_limit: float
    final_dual_multiplier: float
    history: tuple[Mapping[str, float], ...]
    seed: int
    selected_iteration: int
    constraint_satisfied: bool
    minimum_average_gross_pct: float
    minimum_eligible_effective_scale: float
    feasibility: Mapping[str, object]


def evaluate_policy(
    trades,
    universe,
    start_date,
    end_date,
    *,
    price_loader,
    market_benchmark_loader,
    portfolio_policy=None,
    sizing_policy=None,
    decision_log=None,
):
    entry_sizing_policy = (
        None
        if sizing_policy is None
        else sizing_policy.entry_sizing_policy(decision_log)
    )
    return core_portfolio_performance.calculate_score_weighted_performance(
        list(trades),
        list(universe),
        start_date,
        end_date,
        price_loader=price_loader,
        policy=portfolio_policy,
        market_benchmark_loader=market_benchmark_loader,
        entry_sizing_policy=entry_sizing_policy,
    )


def train_policy(
    trades,
    universe,
    start_date,
    end_date,
    *,
    price_loader,
    market_benchmark_loader,
    portfolio_policy=None,
    config=None,
    seed=11,
    progress_callback=None,
):
    """Fit one deterministic continuous policy with antithetic ES.

    Every objective evaluation is a full chronological ledger replay.  The
    optimizer only sees IS results supplied by the caller.
    """

    config = config or EvolutionStrategyConfig()
    if not isinstance(config, EvolutionStrategyConfig):
        raise TypeError("config must be an EvolutionStrategyConfig")
    seed = int(seed)
    if seed < 0:
        raise ValueError("seed must be non-negative")
    rng = np.random.default_rng(seed)

    baseline_performance = evaluate_policy(
        trades,
        universe,
        start_date,
        end_date,
        price_loader=price_loader,
        market_benchmark_loader=market_benchmark_loader,
        portfolio_policy=portfolio_policy,
    )
    baseline_metrics = policy_metrics(
        baseline_performance, confidence=config.cvar_confidence
    )
    if baseline_metrics["filled_trades"] < config.minimum_filled_trades:
        raise ValueError(
            "insufficient filled IS trades for portfolio policy training"
        )
    if (
        baseline_metrics["feature_eligible_filled_trades"]
        < config.minimum_feature_eligible_filled_trades
    ):
        raise ValueError(
            "insufficient feature-eligible filled IS trades for portfolio "
            "policy training"
        )
    baseline_feasibility = policy_feasibility(
        baseline_metrics,
        baseline_metrics,
        config,
    )
    cvar_limit = float(baseline_feasibility["cvar_limit"])
    minimum_average_gross_pct = float(
        baseline_feasibility["minimum_average_gross_pct"]
    )

    theta = ContinuousSizingPolicy.initial().to_vector()
    first_moment = np.zeros_like(theta)
    second_moment = np.zeros_like(theta)
    dual = 1.0
    history = []

    def score(parameters):
        policy = ContinuousSizingPolicy.from_vector(parameters)
        performance = evaluate_policy(
            trades,
            universe,
            start_date,
            end_date,
            price_loader=price_loader,
            market_benchmark_loader=market_benchmark_loader,
            portfolio_policy=portfolio_policy,
            sizing_policy=policy,
        )
        metrics = policy_metrics(
            performance, confidence=config.cvar_confidence
        )
        objective = constrained_objective(
            metrics,
            cvar_limit,
            dual,
            l2_cost=_policy_l2_cost(parameters, config.l2_penalty),
        )
        return objective, metrics

    def is_feasible(metrics):
        return policy_feasibility(
            metrics,
            baseline_metrics,
            config,
        )["satisfied"]

    initial_score, initial_metrics = score(theta)
    best_parameters = theta.copy() if is_feasible(initial_metrics) else None
    best_metrics = (
        dict(initial_metrics) if best_parameters is not None else None
    )
    best_selection_score = (
        float(initial_score) if best_parameters is not None else -math.inf
    )
    selected_iteration = 0 if best_parameters is not None else -1

    for iteration in range(1, config.iterations + 1):
        noises = rng.normal(size=(config.population, theta.size))
        gradient = np.zeros_like(theta)
        pair_scores = []
        for noise in noises:
            plus = np.clip(
                theta + config.noise_std * noise,
                -config.parameter_clip,
                config.parameter_clip,
            )
            minus = np.clip(
                theta - config.noise_std * noise,
                -config.parameter_clip,
                config.parameter_clip,
            )
            plus_score, _ = score(plus)
            minus_score, _ = score(minus)
            gradient += (plus_score - minus_score) * noise
            pair_scores.extend((plus_score, minus_score))
        gradient /= (
            2.0 * config.population * config.noise_std
        )

        first_moment = (
            config.adam_beta1 * first_moment
            + (1.0 - config.adam_beta1) * gradient
        )
        second_moment = (
            config.adam_beta2 * second_moment
            + (1.0 - config.adam_beta2) * (gradient * gradient)
        )
        corrected_first = first_moment / (
            1.0 - config.adam_beta1**iteration
        )
        corrected_second = second_moment / (
            1.0 - config.adam_beta2**iteration
        )
        theta = theta + config.learning_rate * corrected_first / (
            np.sqrt(corrected_second) + config.adam_epsilon
        )
        theta = np.clip(
            theta, -config.parameter_clip, config.parameter_clip
        )

        dual_used = dual
        current_score, current_metrics = score(theta)
        constraint_gap = float(current_metrics["cvar"]) - cvar_limit
        violation = max(0.0, constraint_gap)
        # Signed primal-dual update lets the multiplier relax again after the
        # policy returns to the feasible side of the baseline CVaR constraint.
        dual_next = max(
            0.0,
            dual_used + config.dual_learning_rate * constraint_gap,
        )
        selected_as_best = False
        if (
            is_feasible(current_metrics)
            and float(current_score) > best_selection_score
        ):
            best_parameters = theta.copy()
            best_metrics = dict(current_metrics)
            best_selection_score = float(current_score)
            selected_iteration = iteration
            selected_as_best = True
        row = {
            "iteration": iteration,
            "seed": seed,
            "objective": current_score,
            "pair_score_mean": float(np.mean(pair_scores)),
            "excess_log_return": current_metrics["excess_log_return"],
            "cvar": current_metrics["cvar"],
            "cvar_limit": cvar_limit,
            "cvar_constraint_gap": constraint_gap,
            "cvar_violation": violation,
            "dual_multiplier_used": dual_used,
            "dual_multiplier_next": dual_next,
            "roi_pct": current_metrics["roi_pct"],
            "acwi_roi_pct": current_metrics["acwi_roi_pct"],
            "mdd_pct": current_metrics["mdd_pct"],
            "filled_trades": current_metrics["filled_trades"],
            "average_gross_pct": current_metrics["average_gross_pct"],
            "minimum_average_gross_pct": minimum_average_gross_pct,
            "feature_eligible_filled_trades": current_metrics[
                "feature_eligible_filled_trades"
            ],
            "passthrough_filled_trades": current_metrics[
                "passthrough_filled_trades"
            ],
            "eligible_average_effective_scale": current_metrics[
                "eligible_average_effective_scale"
            ],
            "capital_preservation_ratio": current_metrics[
                "eligible_capital_preservation_ratio"
            ],
            "redistribution_turnover_pct": current_metrics[
                "eligible_redistribution_turnover_pct"
            ],
            "minimum_eligible_effective_scale": (
                config.minimum_eligible_effective_scale
            ),
            "feasible": is_feasible(current_metrics),
            "selected_as_best": selected_as_best,
        }
        dual = dual_next
        history.append(row)
        if progress_callback is not None:
            progress_callback(iteration, config.iterations, dict(row))

    if best_parameters is None or best_metrics is None:
        raise RuntimeError(
            "no trained policy satisfied the CVaR, trade-count, and "
            "minimum-gross constraints"
        )
    policy = ContinuousSizingPolicy.from_vector(best_parameters)
    trained_performance = evaluate_policy(
        trades,
        universe,
        start_date,
        end_date,
        price_loader=price_loader,
        market_benchmark_loader=market_benchmark_loader,
        portfolio_policy=portfolio_policy,
        sizing_policy=policy,
    )
    trained_metrics = policy_metrics(
        trained_performance, confidence=config.cvar_confidence
    )
    constraint_satisfied = is_feasible(trained_metrics)
    if not constraint_satisfied:
        raise RuntimeError(
            "selected policy failed its deterministic feasibility replay"
        )
    final_feasibility = policy_feasibility(
        trained_metrics,
        baseline_metrics,
        config,
    )
    return TrainingResult(
        policy=policy,
        baseline_metrics=dict(baseline_metrics),
        trained_metrics=dict(trained_metrics),
        cvar_limit=cvar_limit,
        final_dual_multiplier=dual,
        history=tuple(history),
        seed=seed,
        selected_iteration=selected_iteration,
        constraint_satisfied=constraint_satisfied,
        minimum_average_gross_pct=minimum_average_gross_pct,
        minimum_eligible_effective_scale=(
            config.minimum_eligible_effective_scale
        ),
        feasibility=final_feasibility,
    )
