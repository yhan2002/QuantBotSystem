"""Point-in-time market-regime routing for Portfolio Beta research.

The overlay changes only new-entry requested weights through the ledger's
existing entry-sizing hook.  Strategy signals, exits, quantities already held,
and every allocator cap remain outside this module.
"""

from __future__ import annotations

import math
from typing import Mapping

from hmmlearn.hmm import GaussianHMM
import numpy as np
import pandas as pd


FEATURE_COLUMNS = (
    "market_return_20d",
    "realized_volatility_20d",
    "trend_strength_20d",
    "breadth_20d",
    "dispersion_20d",
)
TREND_FEATURE = "trend_strength_20d"


def _normalized_index(values):
    index = pd.to_datetime(values, errors="coerce").normalize()
    if index.isna().any():
        raise ValueError("regime data contains an invalid date")
    return index


def _close_series(values, label):
    if isinstance(values, pd.DataFrame):
        columns = {str(column).lower(): column for column in values.columns}
        if "close" not in columns:
            raise ValueError(f"{label} must contain a Close column")
        values = values[columns["close"]]
    series = pd.to_numeric(pd.Series(values).copy(), errors="coerce")
    series.index = _normalized_index(series.index)
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series.where(series > 0.0)


def _close_frame(values):
    if not isinstance(values, pd.DataFrame) or values.empty:
        raise ValueError("universe_close must be a non-empty DataFrame")
    frame = values.apply(pd.to_numeric, errors="coerce").copy()
    frame.index = _normalized_index(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.where(frame > 0.0)


def build_regime_features(
    market_close,
    universe_close,
    *,
    lookback=20,
    min_constituents=2,
    annualization=252,
):
    """Build close-known market features without backfilling missing prices."""

    if int(lookback) < 2:
        raise ValueError("lookback must be at least 2")
    if int(min_constituents) < 1:
        raise ValueError("min_constituents must be positive")
    market = _close_series(market_close, "market_close")
    universe = _close_frame(universe_close)
    lookback = int(lookback)

    daily_market_return = market.pct_change(fill_method=None)
    market_return = market.pct_change(lookback, fill_method=None)
    realized_volatility = (
        daily_market_return.rolling(lookback, min_periods=lookback).std(ddof=0)
        * math.sqrt(float(annualization))
    )
    lookback_volatility = realized_volatility * math.sqrt(
        lookback / float(annualization)
    )
    trend_strength = market_return.abs().div(
        lookback_volatility.where(lookback_volatility > 0.0)
    )

    universe_return = universe.pct_change(lookback, fill_method=None)
    constituent_count = universe_return.notna().sum(axis=1)
    breadth = universe_return.gt(0.0).sum(axis=1).div(constituent_count)
    dispersion = universe_return.std(axis=1, ddof=0, skipna=True)
    breadth = breadth.where(constituent_count >= int(min_constituents))
    dispersion = dispersion.where(constituent_count >= int(min_constituents))

    features = pd.concat(
        {
            "market_return_20d": market_return,
            "realized_volatility_20d": realized_volatility,
            "trend_strength_20d": trend_strength,
            "breadth_20d": breadth,
            "dispersion_20d": dispersion,
        },
        axis=1,
    )
    return features.replace([np.inf, -np.inf], np.nan).dropna()


def rule_trend_probabilities(features, *, min_history=60):
    """Past-only TREND/NON_TREND comparator used before judging the HMM."""

    frame = _feature_frame(features)
    minimum = int(min_history)
    if minimum < 2:
        raise ValueError("min_history must be at least 2")
    trend = frame[TREND_FEATURE]
    volatility = frame["realized_volatility_20d"]
    trend_threshold = trend.expanding(min_periods=minimum).median().shift(1)
    stress_threshold = (
        volatility.expanding(min_periods=minimum).quantile(0.75).shift(1)
    )
    probability = pd.Series(0.5, index=frame.index, name="p_trend")
    eligible = trend_threshold.notna() & stress_threshold.notna()
    probability.loc[eligible] = (
        (trend.loc[eligible] >= trend_threshold.loc[eligible])
        & (volatility.loc[eligible] <= stress_threshold.loc[eligible])
    ).astype(float)
    return probability


def _feature_frame(features):
    if not isinstance(features, pd.DataFrame):
        raise TypeError("features must be a DataFrame")
    missing = [column for column in FEATURE_COLUMNS if column not in features]
    if missing:
        raise ValueError("missing regime features: " + ", ".join(missing))
    frame = features.loc[:, FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    frame.index = _normalized_index(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    if frame.empty:
        raise ValueError("features contain no complete observations")
    return frame


def walk_forward_trend_probabilities(
    features,
    *,
    min_train_size=252,
    refit_interval=21,
    train_window=None,
):
    """Return filtered probabilities; every row uses only earlier fit data."""

    frame = _feature_frame(features)
    minimum = int(min_train_size)
    interval = int(refit_interval)
    if minimum < 10:
        raise ValueError("min_train_size must be at least 10")
    if interval < 1:
        raise ValueError("refit_interval must be positive")
    if train_window is not None and int(train_window) < minimum:
        raise ValueError("train_window cannot be shorter than min_train_size")

    raw = frame.to_numpy(dtype=np.float64)
    output = pd.Series(np.nan, index=frame.index, name="p_trend")
    trend_index = FEATURE_COLUMNS.index(TREND_FEATURE)
    model = mean = scale = None
    trend_state = None
    last_fit = None
    fit_start = 0

    for index in range(minimum, len(frame)):
        if model is None or index - last_fit >= interval:
            fit_start = (
                0 if train_window is None else max(0, index - int(train_window))
            )
            training = raw[fit_start:index]
            mean = training.mean(axis=0)
            scale = training.std(axis=0, ddof=0)
            scale = np.where(scale > 1e-12, scale, 1.0)
            standardized = (training - mean) / scale
            trend = standardized[:, trend_index]
            groups = (trend > np.median(trend)).astype(int)
            if groups.min() == groups.max():
                groups = np.arange(len(training)) % 2
            model = GaussianHMM(
                n_components=2,
                covariance_type="diag",
                min_covar=1e-4,
                n_iter=100,
                tol=1e-6,
                random_state=0,
                implementation="log",
                init_params="",
            )
            model.startprob_ = np.array([0.5, 0.5])
            model.transmat_ = np.array([[0.95, 0.05], [0.05, 0.95]])
            model.means_ = np.vstack(
                [standardized[groups == state].mean(axis=0) for state in range(2)]
            )
            model.covars_ = np.maximum(
                np.vstack(
                    [standardized[groups == state].var(axis=0) for state in range(2)]
                ),
                1e-4,
            )
            model.fit(standardized)
            history = list(model.monitor_.history)
            if len(history) > 1 and history[-1] < history[-2] - 1e-6:
                raise RuntimeError("HMM likelihood decreased during fitting")
            filtered = model.predict_proba(standardized)
            state_weight = filtered.sum(axis=0)
            state_trend = (
                filtered.T @ training[:, trend_index]
                / np.maximum(state_weight, 1e-12)
            )
            trend_state = int(np.argmax(state_trend))
            last_fit = index
        observed = (raw[fit_start : index + 1] - mean) / scale
        output.iloc[index] = float(model.predict_proba(observed)[-1, trend_state])
    return output


def _strategy_bucket(value):
    key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if key in {"MA", "MA_CROSS", "MA_MOMENTUM", "MOMENTUM"}:
        return "TREND"
    if key in {"BOX", "BOX_RANGE", "REVERSION", "VALUE_REVERSION"}:
        return "NON_TREND"
    return None


def _capped_targets(weights, preferences, cap):
    weights = np.asarray(weights, dtype=np.float64)
    scores = weights * np.asarray(preferences, dtype=np.float64)
    target_total = float(weights.sum())
    if target_total > float(cap) * len(weights) + 1e-12:
        raise ValueError("requested cohort gross exceeds the name-cap capacity")
    if float(scores.sum()) <= 0.0:
        return weights.copy()
    targets = np.zeros_like(weights)
    active = np.ones(len(weights), dtype=bool)
    remaining = target_total
    while active.any():
        active_scores = scores[active]
        if float(active_scores.sum()) <= 0.0:
            targets[active] = remaining / int(active.sum())
            break
        proposed = active_scores * (remaining / float(active_scores.sum()))
        capped = proposed > float(cap)
        active_indices = np.flatnonzero(active)
        if not capped.any():
            targets[active_indices] = proposed
            break
        capped_indices = active_indices[capped]
        targets[capped_indices] = float(cap)
        active[capped_indices] = False
        remaining -= float(cap) * len(capped_indices)
    return targets


class RegimeSizingPolicy:
    """Redistribute each side's entry cohort by point-in-time regime odds."""

    def __init__(
        self,
        trend_probabilities,
        *,
        floor=0.25,
        max_name_weight=0.05,
    ):
        floor = float(floor)
        if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
            raise ValueError("floor must be between 0 and 1")
        max_name_weight = float(max_name_weight)
        if (
            not math.isfinite(max_name_weight)
            or not 0.0 < max_name_weight <= 1.0
        ):
            raise ValueError("max_name_weight must be between 0 and 1")
        if isinstance(trend_probabilities, pd.Series):
            items = trend_probabilities.items()
        elif isinstance(trend_probabilities, Mapping):
            items = trend_probabilities.items()
        else:
            raise TypeError("trend_probabilities must be a Series or mapping")
        probabilities = {}
        for date, value in items:
            if pd.isna(value):
                continue
            probability = float(value)
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise ValueError("trend probabilities must be between 0 and 1")
            probabilities[pd.Timestamp(date).normalize()] = probability
        self.probabilities = probabilities
        self.floor = floor
        self.max_name_weight = max_name_weight

    def entry_sizing_policy(self, decision_log=None):
        if decision_log is None:
            return self

        def resolve(*, signals, portfolio_state):
            decision = self(signals=signals, portfolio_state=portfolio_state)
            for signal in signals:
                trade_id = str(signal["trade_id"])
                decision_log.append(
                    {
                        "signal_date": str(signal.get("signal_date") or ""),
                        "trade_id": trade_id,
                        "strategy": signal.get("strategy"),
                        "trade_multiplier": decision["trade_multipliers"][trade_id],
                    }
                )
            return decision

        return resolve

    def __call__(self, *, signals, portfolio_state):
        del portfolio_state
        multipliers = {str(signal["trade_id"]): 1.0 for signal in signals}
        eligible_by_side = {"LONG": [], "SHORT": []}
        for signal in signals:
            bucket = _strategy_bucket(signal.get("strategy"))
            date_value = signal.get("signal_date")
            if bucket is None or date_value is None:
                continue
            probability = self.probabilities.get(pd.Timestamp(date_value).normalize())
            if probability is None:
                continue
            regime_probability = (
                probability if bucket == "TREND" else 1.0 - probability
            )
            raw_multiplier = self.floor + (1.0 - self.floor) * regime_probability
            weight = max(0.0, float(signal.get("requested_weight", 0.0)))
            side = str(signal.get("side") or "").upper()
            if side in eligible_by_side and weight > 0.0:
                eligible_by_side[side].append(
                    (str(signal["trade_id"]), weight, raw_multiplier)
                )

        for items in eligible_by_side.values():
            if len(items) < 2:
                continue
            targets = _capped_targets(
                [weight for _, weight, _ in items],
                [multiplier for _, _, multiplier in items],
                self.max_name_weight,
            )
            for (trade_id, weight, _), target in zip(items, targets):
                multipliers[trade_id] = float(target) / weight
        return {"gross_scale": 1.0, "trade_multipliers": multipliers}
