"""Deterministic residual statistical-arbitrage research engine.

The module is deliberately data-source agnostic.  Every public analyzer,
scanner, and backtest receives an OHLCV ``price_map`` and security metadata
from its caller; it never downloads data or reads the shared market database.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from strategy_residual_stat_arb_cache import ResidualCacheContext
except ImportError:  # pragma: no cover - package-style import fallback
    from .strategy_residual_stat_arb_cache import ResidualCacheContext


STRATEGY_KEY = "residual_stat_arb"
NEXT_OPEN = "NEXT_OPEN"
EVALUATION_END_MARK = "EVALUATION_END_MARK"


@dataclass(frozen=True)
class ResidualStatArbConfig:
    """Parameters for the deterministic first version."""

    train_window: int = 120
    min_train_observations: int = 60
    residual_horizon: int = 5
    z_window: int = 60
    entry_extreme_z: float = 2.0
    residual_stop_z: float = 3.5
    max_holding_days: int = 10
    max_peers: int = 5
    peer_correlation_lookback: int = 120
    peer_min_observations: int = 60
    max_hedge_beta: float = 3.0

    def __post_init__(self):
        if self.train_window < 2:
            raise ValueError("train_window must be at least 2")
        if not 2 <= self.min_train_observations <= self.train_window:
            raise ValueError(
                "min_train_observations must be between 2 and train_window"
            )
        if self.residual_horizon < 1:
            raise ValueError("residual_horizon must be positive")
        if self.z_window < 2:
            raise ValueError("z_window must be at least 2")
        if self.entry_extreme_z <= 0:
            raise ValueError("entry_extreme_z must be positive")
        if self.residual_stop_z <= self.entry_extreme_z:
            raise ValueError("residual_stop_z must exceed entry_extreme_z")
        if self.max_holding_days < 1:
            raise ValueError("max_holding_days must be positive")
        if self.max_peers < 1:
            raise ValueError("max_peers must be positive")
        if self.peer_correlation_lookback < 2:
            raise ValueError("peer_correlation_lookback must be at least 2")
        if not 2 <= self.peer_min_observations <= self.peer_correlation_lookback:
            raise ValueError(
                "peer_min_observations must be between 2 and "
                "peer_correlation_lookback"
            )
        if self.max_hedge_beta <= 0:
            raise ValueError("max_hedge_beta must be positive")


DEFAULT_CONFIG = ResidualStatArbConfig()
_OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


class NormalizedPriceMap(dict):
    """Canonical price mapping with a lazy daily-return cache."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._return_cache = {}

    def returns(self, ticker):
        if ticker not in self._return_cache:
            self._return_cache[ticker] = _returns(self[ticker])
        return self._return_cache[ticker]


def _config_or_default(config):
    if config is None:
        return DEFAULT_CONFIG
    if not isinstance(config, ResidualStatArbConfig):
        raise TypeError("config must be a ResidualStatArbConfig")
    return config


def create_residual_cache_context(
    price_map,
    metadata_by_ticker,
    **kwargs,
):
    """Create one reusable cache context for a scan/backtest job."""

    return ResidualCacheContext(
        normalize_price_map(price_map),
        metadata_by_ticker,
        **kwargs,
    )


def _cache_key(cache_context, method_name, *args):
    if cache_context is None:
        return None
    try:
        return getattr(cache_context, method_name)(*args)
    except Exception:
        return None


def _cache_get(cache_context, namespace, cache_key):
    if cache_context is None or cache_key is None:
        return None
    try:
        return cache_context.get(namespace, cache_key)
    except Exception:
        return None


def _cache_put(cache_context, namespace, cache_key, value):
    if cache_context is None or cache_key is None:
        return
    try:
        cache_context.put(namespace, cache_key, value)
    except Exception:
        return


def normalize_ohlcv(prices):
    """Return canonical, sorted OHLCV data or an empty frame.

    A single-security yfinance-style MultiIndex is supported when one of its
    levels contains the OHLCV field names.
    """

    if not isinstance(prices, pd.DataFrame) or prices.empty:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    clean = prices.copy()
    if isinstance(clean.columns, pd.MultiIndex):
        selected_level = None
        required_lower = {name.lower() for name in _OHLCV_COLUMNS}
        for level in range(clean.columns.nlevels):
            values = {str(value).strip().lower() for value in clean.columns.get_level_values(level)}
            if required_lower.issubset(values):
                selected_level = level
                break
        if selected_level is None:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)
        clean.columns = clean.columns.get_level_values(selected_level)

    lower_to_original = {}
    for column in clean.columns:
        lower_to_original.setdefault(str(column).strip().lower(), column)
    if any(name.lower() not in lower_to_original for name in _OHLCV_COLUMNS):
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    clean = clean[
        [lower_to_original[name.lower()] for name in _OHLCV_COLUMNS]
    ].copy()
    clean.columns = list(_OHLCV_COLUMNS)
    for column in _OHLCV_COLUMNS:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")

    index = pd.to_datetime(clean.index, errors="coerce")
    valid_index = ~pd.isna(index)
    clean = clean.loc[valid_index].copy()
    index = pd.DatetimeIndex(index[valid_index])
    if index.tz is not None:
        index = index.tz_convert(None)
    clean.index = index
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    clean = clean.dropna(subset=list(_OHLCV_COLUMNS))
    clean = clean[(clean["Open"] > 0) & (clean["Close"] > 0)]
    return clean.astype(float)


def normalize_price_map(price_map):
    """Normalize a caller-supplied ticker-to-OHLCV mapping."""

    if isinstance(price_map, NormalizedPriceMap):
        return price_map
    if not hasattr(price_map, "items"):
        return NormalizedPriceMap()
    normalized = NormalizedPriceMap()
    for ticker, prices in price_map.items():
        clean = normalize_ohlcv(prices)
        if not clean.empty:
            normalized[str(ticker)] = clean
    return normalized


def _metadata_value(metadata_by_ticker, ticker, field):
    row = (metadata_by_ticker or {}).get(ticker, {})
    value = row.get(field) if hasattr(row, "get") else None
    return str(value).strip().casefold() if value is not None else ""


def _same_peer_group(ticker, candidate, metadata_by_ticker):
    target_country = _metadata_value(
        metadata_by_ticker, ticker, "listing_country"
    )
    target_sector = _metadata_value(metadata_by_ticker, ticker, "sector")
    return bool(
        target_country
        and target_sector
        and target_country
        == _metadata_value(metadata_by_ticker, candidate, "listing_country")
        and target_sector
        == _metadata_value(metadata_by_ticker, candidate, "sector")
    )


def _returns(prices):
    return prices["Close"].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)


def _cached_returns(price_map, ticker):
    if isinstance(price_map, NormalizedPriceMap):
        return price_map.returns(ticker)
    return _returns(price_map[ticker])


def _resolve_as_of(target_prices, as_of):
    if target_prices.empty:
        return None
    if as_of is None:
        return target_prices.index[-1]
    value = pd.Timestamp(as_of)
    if value.tzinfo is not None:
        value = value.tz_convert(None)
    eligible = target_prices.index[target_prices.index <= value]
    return eligible[-1] if len(eligible) else None


def rank_peer_candidates(
    ticker,
    price_map,
    metadata_by_ticker,
    as_of=None,
    config=None,
    cache_context=None,
):
    """Rank same-country and same-sector peers using returns strictly before as_of."""

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    target = normalized.get(ticker)
    if target is None:
        return []
    signal_date = _resolve_as_of(target, as_of)
    if signal_date is None:
        return []
    cache_key = _cache_key(
        cache_context,
        "peer_ranking_key",
        ticker,
        signal_date,
        config,
    )
    cached = _cache_get(cache_context, "peer_ranking", cache_key)
    if isinstance(cached, list):
        return cached

    target_returns = _cached_returns(normalized, ticker)
    target_returns = target_returns[target_returns.index < signal_date]
    ranked = []
    for candidate in sorted(normalized):
        if candidate == ticker or not _same_peer_group(
            ticker, candidate, metadata_by_ticker
        ):
            continue
        candidate_returns = _cached_returns(normalized, candidate)
        aligned = pd.concat(
            [target_returns.rename("target"), candidate_returns.rename("peer")],
            axis=1,
            join="inner",
        ).dropna()
        aligned = aligned.iloc[-config.peer_correlation_lookback :]
        if len(aligned) < config.peer_min_observations:
            continue
        correlation = float(aligned["target"].corr(aligned["peer"]))
        if not np.isfinite(correlation):
            continue
        ranked.append(
            {
                "ticker": candidate,
                "correlation": correlation,
                "observations": int(len(aligned)),
                "correlation_end": aligned.index[-1].date().isoformat(),
            }
        )
    ranked = sorted(
        ranked, key=lambda row: (-row["correlation"], row["ticker"])
    )
    _cache_put(cache_context, "peer_ranking", cache_key, ranked)
    return ranked


def select_residual_peers(
    ticker,
    price_map,
    metadata_by_ticker,
    as_of=None,
    config=None,
    cache_context=None,
):
    """Select at most five trailing-correlation peers by default."""

    config = _config_or_default(config)
    return [
        row["ticker"]
        for row in rank_peer_candidates(
            ticker,
            price_map,
            metadata_by_ticker,
            as_of=as_of,
            config=config,
            cache_context=cache_context,
        )[: config.max_peers]
    ]


def build_factor_returns(
    ticker,
    price_map,
    metadata_by_ticker,
    peers=None,
    as_of=None,
    config=None,
    cache_context=None,
):
    """Build target, equal-weight country-market, and peer-excess returns."""

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    target = normalized.get(ticker)
    if target is None:
        return pd.DataFrame()
    signal_date = _resolve_as_of(target, as_of)
    if signal_date is None:
        return pd.DataFrame()
    target = target.loc[:signal_date]

    if peers is None:
        peers = select_residual_peers(
            ticker,
            normalized,
            metadata_by_ticker,
            as_of=signal_date,
            config=config,
            cache_context=cache_context,
        )
    peers = tuple(
        candidate
        for candidate in peers
        if candidate in normalized
        and candidate != ticker
        and _same_peer_group(ticker, candidate, metadata_by_ticker)
    )
    if not peers:
        return pd.DataFrame()

    target_country = _metadata_value(
        metadata_by_ticker, ticker, "listing_country"
    )
    market_tickers = [
        candidate
        for candidate in sorted(normalized)
        if candidate != ticker
        and _metadata_value(
            metadata_by_ticker, candidate, "listing_country"
        )
        == target_country
    ]
    if not target_country or not market_tickers:
        return pd.DataFrame()

    target_index = target.index
    market_columns = [
        _cached_returns(normalized, candidate).reindex(target_index).rename(candidate)
        for candidate in market_tickers
    ]
    peer_columns = [
        _cached_returns(normalized, candidate).reindex(target_index).rename(candidate)
        for candidate in peers
    ]
    market_return = pd.concat(market_columns, axis=1).mean(
        axis=1, skipna=True
    )
    # A peer basket is investable only when every selected leg has that return.
    peer_return = pd.concat(peer_columns, axis=1).mean(
        axis=1, skipna=False
    )
    factors = pd.DataFrame(
        {
            "TARGET_RETURN": _cached_returns(normalized, ticker).reindex(target_index),
            "MARKET_RETURN": market_return,
            "PEER_RETURN": peer_return,
        },
        index=target_index,
    )
    factors["SECTOR_EXCESS_RETURN"] = (
        factors["PEER_RETURN"] - factors["MARKET_RETURN"]
    )
    factors.attrs["ticker"] = ticker
    factors.attrs["peers"] = peers
    factors.attrs["market_tickers"] = tuple(market_tickers)
    return factors.replace([np.inf, -np.inf], np.nan)


def build_residual_features(
    ticker,
    price_map,
    metadata_by_ticker,
    peers=None,
    as_of=None,
    config=None,
    cache_context=None,
):
    """Estimate lagged rolling betas and close-known residual signals.

    For row ``t``, every coefficient is estimated from rows ending at ``t-1``.
    The row-t return is used only after those coefficients have been fixed.
    """

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    target = normalized.get(ticker)
    signal_date = (
        _resolve_as_of(target, as_of) if target is not None else None
    )
    if target is not None and signal_date is None:
        return pd.DataFrame()
    if peers is None:
        peers = select_residual_peers(
            ticker,
            normalized,
            metadata_by_ticker,
            as_of=signal_date if signal_date is not None else as_of,
            config=config,
            cache_context=cache_context,
        )
    peers = tuple(
        candidate
        for candidate in peers
        if candidate in normalized
        and candidate != ticker
        and _same_peer_group(ticker, candidate, metadata_by_ticker)
    )
    cache_key = _cache_key(
        cache_context,
        "residual_features_key",
        ticker,
        signal_date if signal_date is not None else as_of,
        config,
        peers,
    )
    cached = _cache_get(cache_context, "residual_features", cache_key)
    if isinstance(cached, pd.DataFrame):
        return cached

    factors = build_factor_returns(
        ticker,
        normalized,
        metadata_by_ticker,
        peers=peers,
        as_of=signal_date if signal_date is not None else as_of,
        config=config,
        cache_context=cache_context,
    )
    if factors.empty:
        return factors

    features = factors.copy()
    size = len(features)
    alpha = np.full(size, np.nan)
    beta_market = np.full(size, np.nan)
    beta_sector = np.full(size, np.nan)
    hedge_alpha = np.full(size, np.nan)
    hedge_beta_raw = np.full(size, np.nan)
    hedge_beta = np.full(size, np.nan)
    residual = np.full(size, np.nan)
    observations = np.zeros(size, dtype=int)
    train_end = np.full(
        size, np.datetime64("NaT", "ns"), dtype="datetime64[ns]"
    )
    columns = [
        "TARGET_RETURN",
        "MARKET_RETURN",
        "SECTOR_EXCESS_RETURN",
        "PEER_RETURN",
    ]

    for position in range(size):
        start = max(0, position - config.train_window)
        # The current row is deliberately excluded here.
        training = features.iloc[start:position][columns].dropna()
        if len(training) < config.min_train_observations:
            continue
        y = training["TARGET_RETURN"].to_numpy(dtype=float)
        x = training[
            ["MARKET_RETURN", "SECTOR_EXCESS_RETURN"]
        ].to_numpy(dtype=float)
        design = np.column_stack([np.ones(len(training)), x])
        coefficients = np.linalg.lstsq(design, y, rcond=None)[0]
        alpha[position], beta_market[position], beta_sector[position] = coefficients
        observations[position] = len(training)
        train_end[position] = np.datetime64(training.index[-1].to_datetime64())

        peer_values = training["PEER_RETURN"].to_numpy(dtype=float)
        centered_peer = peer_values - peer_values.mean()
        denominator = float(np.dot(centered_peer, centered_peer))
        if denominator > np.finfo(float).eps:
            direct_beta = float(
                np.dot(centered_peer, y - y.mean()) / denominator
            )
            hedge_beta_raw[position] = direct_beta
            # The executable hedge is the same peer basket used here. A
            # non-positive beta would require a same-direction "hedge" and is
            # therefore rejected rather than hidden behind abs(beta).
            if direct_beta > 0:
                capped_beta = min(direct_beta, config.max_hedge_beta)
                hedge_alpha[position] = float(
                    y.mean() - capped_beta * peer_values.mean()
                )
                hedge_beta[position] = capped_beta

        current = features.iloc[position]
        executable_values = current[
            ["TARGET_RETURN", "PEER_RETURN"]
        ].to_numpy(dtype=float)
        if (
            np.isfinite(executable_values).all()
            and np.isfinite(hedge_alpha[position])
            and np.isfinite(hedge_beta[position])
        ):
            fitted = (
                hedge_alpha[position]
                + hedge_beta[position] * current["PEER_RETURN"]
            )
            residual[position] = float(current["TARGET_RETURN"] - fitted)

    features["ALPHA"] = alpha
    features["BETA_MARKET"] = beta_market
    features["BETA_SECTOR_EXCESS"] = beta_sector
    features["HEDGE_ALPHA"] = hedge_alpha
    features["HEDGE_BETA_RAW"] = hedge_beta_raw
    features["HEDGE_BETA"] = hedge_beta
    features["BETA_OBSERVATIONS"] = observations
    features["BETA_TRAIN_END"] = pd.to_datetime(train_end)
    features["RESIDUAL"] = residual
    features["RESIDUAL_CUM"] = features["RESIDUAL"].rolling(
        config.residual_horizon,
        min_periods=config.residual_horizon,
    ).sum()

    prior_cumulative = features["RESIDUAL_CUM"].shift(1)
    prior_mean = prior_cumulative.rolling(
        config.z_window, min_periods=config.z_window
    ).mean()
    prior_std = prior_cumulative.rolling(
        config.z_window, min_periods=config.z_window
    ).std(ddof=0)
    features["RESIDUAL_Z_MEAN"] = prior_mean
    features["RESIDUAL_Z_STD"] = prior_std
    features["RESIDUAL_Z"] = (
        (features["RESIDUAL_CUM"] - prior_mean)
        / prior_std.replace(0.0, np.nan)
    )
    features["PREVIOUS_RESIDUAL_Z"] = features["RESIDUAL_Z"].shift(1)
    features["LONG_SIGNAL"] = (
        (features["PREVIOUS_RESIDUAL_Z"] <= -config.entry_extreme_z)
        & (features["RESIDUAL_Z"] > -config.entry_extreme_z)
    ).fillna(False)
    features["SHORT_SIGNAL"] = (
        (features["PREVIOUS_RESIDUAL_Z"] >= config.entry_extreme_z)
        & (features["RESIDUAL_Z"] < config.entry_extreme_z)
    ).fillna(False)
    features["SIGNAL_SCORE"] = np.where(
        features["LONG_SIGNAL"],
        features["PREVIOUS_RESIDUAL_Z"].abs(),
        np.where(
            features["SHORT_SIGNAL"],
            -features["PREVIOUS_RESIDUAL_Z"].abs(),
            0.0,
        ),
    )
    _cache_put(
        cache_context,
        "residual_features",
        cache_key,
        features,
    )
    return features


def build_trade_legs(ticker, peers, side, hedge_beta):
    """Create signed target and equal-weight beta-adjusted peer ratios."""

    side = str(side).strip().upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    peers = tuple(dict.fromkeys(str(peer) for peer in peers if str(peer) != ticker))
    if not peers:
        raise ValueError("at least one peer is required")
    hedge_beta = float(hedge_beta)
    if not np.isfinite(hedge_beta) or hedge_beta < 0:
        raise ValueError("hedge_beta must be finite and non-negative")

    direction = 1.0 if side == "LONG" else -1.0
    per_peer_ratio = -direction * hedge_beta / len(peers)
    legs = [
        {
            "ticker": ticker,
            "role": "TARGET",
            "side": side,
            "signed_ratio": direction,
        }
    ]
    peer_side = "SHORT" if side == "LONG" else "LONG"
    legs.extend(
        {
            "ticker": peer,
            "role": "PEER_HEDGE",
            "side": peer_side,
            "signed_ratio": per_peer_ratio,
        }
        for peer in peers
    )
    return legs


def summarize_leg_ratios(legs):
    ratios = [float(leg["signed_ratio"]) for leg in legs]
    return {
        "gross_ratio": float(sum(abs(ratio) for ratio in ratios)),
        "net_ratio": float(sum(ratios)),
    }


def _analysis_result_from_features(
    ticker,
    signal_date,
    features,
    ranked_peers,
    config,
    peer_selection_date=None,
):
    peers = tuple(row["ticker"] for row in ranked_peers[: config.max_peers])
    if signal_date not in features.index:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "reason": "signal date is absent from aligned factor data",
        }
    latest = features.loc[signal_date]
    residual_z = latest["RESIDUAL_Z"]
    if not np.isfinite(residual_z):
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "signal_date": signal_date.date().isoformat(),
            "peer_selection_date": (
                peer_selection_date.date().isoformat()
                if peer_selection_date is not None
                else signal_date.date().isoformat()
            ),
            "reason": "residual z-score warmup is incomplete",
            "selected_peers": list(peers),
        }

    result = {
        "status": "NO_SIGNAL",
        "strategy": STRATEGY_KEY,
        "ticker": ticker,
        "signal_date": signal_date.date().isoformat(),
        "peer_selection_date": (
            peer_selection_date.date().isoformat()
            if peer_selection_date is not None
            else signal_date.date().isoformat()
        ),
        "entry_execution": NEXT_OPEN,
        "selected_peers": list(peers),
        "peer_correlations": [
            dict(row) for row in ranked_peers[: config.max_peers]
        ],
        "residual": float(latest["RESIDUAL"]),
        "cumulative_residual": float(latest["RESIDUAL_CUM"]),
        "residual_z": float(residual_z),
        "previous_residual_z": (
            float(latest["PREVIOUS_RESIDUAL_Z"])
            if np.isfinite(latest["PREVIOUS_RESIDUAL_Z"])
            else None
        ),
        "beta_market": float(latest["BETA_MARKET"]),
        "beta_sector_excess": float(latest["BETA_SECTOR_EXCESS"]),
        "hedge_alpha": (
            float(latest["HEDGE_ALPHA"])
            if np.isfinite(latest["HEDGE_ALPHA"])
            else None
        ),
        "hedge_beta": (
            float(latest["HEDGE_BETA"])
            if np.isfinite(latest["HEDGE_BETA"])
            else None
        ),
        "residual_z_mean": (
            float(latest["RESIDUAL_Z_MEAN"])
            if np.isfinite(latest["RESIDUAL_Z_MEAN"])
            else None
        ),
        "residual_z_std": (
            float(latest["RESIDUAL_Z_STD"])
            if np.isfinite(latest["RESIDUAL_Z_STD"])
            else None
        ),
        "beta_observations": int(latest["BETA_OBSERVATIONS"]),
        "beta_train_end": (
            latest["BETA_TRAIN_END"].date().isoformat()
            if pd.notna(latest["BETA_TRAIN_END"])
            else None
        ),
        "signal_score": 0.0,
        "score": 0.0,
    }
    if bool(latest["LONG_SIGNAL"]):
        side = "LONG"
    elif bool(latest["SHORT_SIGNAL"]):
        side = "SHORT"
    else:
        return result
    if (
        result["hedge_alpha"] is None
        or result["hedge_beta"] is None
        or result["residual_z_mean"] is None
        or result["residual_z_std"] is None
        or result["residual_z_std"] <= 0
    ):
        result["status"] = "DATA_INSUFFICIENT"
        result["reason"] = "fixed hedge model or residual z baseline is unavailable"
        return result

    result["status"] = "OK"
    result["side"] = side
    result["signal_score"] = float(latest["SIGNAL_SCORE"])
    result["score"] = result["signal_score"]
    result["legs"] = build_trade_legs(
        ticker, peers, side, result["hedge_beta"]
    )
    result.update(summarize_leg_ratios(result["legs"]))
    return result


def analyze_residual_stat_arb(
    ticker,
    price_map,
    metadata_by_ticker,
    as_of=None,
    peer_selection_as_of=None,
    config=None,
    cache_context=None,
):
    """Analyze one ticker from supplied local data without any I/O."""

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    target = normalized.get(ticker)
    if target is None:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "reason": "target OHLCV is unavailable",
        }
    signal_date = _resolve_as_of(target, as_of)
    if signal_date is None:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "reason": "no target row exists on or before as_of",
        }
    if (
        peer_selection_as_of is not None
        and pd.Timestamp(peer_selection_as_of) > signal_date
    ):
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "signal_date": signal_date.date().isoformat(),
            "reason": "signal date predates the fixed peer-selection cutoff",
        }
    selection_cutoff = (
        signal_date
        if peer_selection_as_of is None
        else pd.Timestamp(peer_selection_as_of)
    )
    peer_selection_date = _resolve_as_of(target, selection_cutoff)
    if peer_selection_date is None:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "signal_date": signal_date.date().isoformat(),
            "reason": "no target row exists on or before peer_selection_as_of",
        }
    ranked = rank_peer_candidates(
        ticker,
        normalized,
        metadata_by_ticker,
        as_of=peer_selection_date,
        config=config,
        cache_context=cache_context,
    )
    if not ranked:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "signal_date": signal_date.date().isoformat(),
            "peer_selection_date": peer_selection_date.date().isoformat(),
            "reason": "no eligible same-country and same-sector peer",
        }
    peers = [row["ticker"] for row in ranked[: config.max_peers]]
    features = build_residual_features(
        ticker,
        normalized,
        metadata_by_ticker,
        peers=peers,
        as_of=signal_date,
        config=config,
        cache_context=cache_context,
    )
    if features.empty:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "signal_date": signal_date.date().isoformat(),
            "reason": "aligned market and peer factors are unavailable",
        }
    return _analysis_result_from_features(
        ticker,
        signal_date,
        features,
        ranked,
        config,
        peer_selection_date=peer_selection_date,
    )


def scan_residual_stat_arb(
    universe_list,
    price_map,
    metadata_by_ticker,
    as_of=None,
    peer_selection_as_of=None,
    config=None,
    limit_per_side=3,
    cache_context=None,
    progress_callback=None,
):
    """Return structured long/short candidates; perform no downloads."""

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    universe_list = list(universe_list)
    total = len(universe_list)
    results = []
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            row = analyze_residual_stat_arb(
                ticker,
                normalized,
                metadata_by_ticker,
                as_of=as_of,
                peer_selection_as_of=peer_selection_as_of,
                config=config,
                cache_context=cache_context,
            )
            results.append(row)
            succeeded = row.get("status") in {"OK", "NO_SIGNAL"}
        finally:
            if progress_callback is not None:
                try:
                    progress_callback(
                        completed,
                        total,
                        ticker,
                        succeeded,
                    )
                except Exception:
                    pass
    signals = [row for row in results if row["status"] == "OK"]
    longs = sorted(
        (row for row in signals if row["side"] == "LONG"),
        key=lambda row: (-row["signal_score"], row["ticker"]),
    )
    shorts = sorted(
        (row for row in signals if row["side"] == "SHORT"),
        key=lambda row: (row["signal_score"], row["ticker"]),
    )
    if limit_per_side is not None:
        longs = longs[: int(limit_per_side)]
        shorts = shorts[: int(limit_per_side)]
    return {
        "strategy": STRATEGY_KEY,
        "as_of": str(as_of) if as_of is not None else None,
        "peer_selection_as_of": (
            str(peer_selection_as_of)
            if peer_selection_as_of is not None
            else None
        ),
        "longs": longs,
        "shorts": shorts,
        "signals": longs + shorts,
        "insufficient": [
            row for row in results if row["status"] == "DATA_INSUFFICIENT"
        ],
        "no_signal": [
            row for row in results if row["status"] == "NO_SIGNAL"
        ],
    }


def build_fixed_hedge_residual_z(
    features,
    hedge_alpha,
    hedge_beta,
    residual_z_mean,
    residual_z_std,
    residual_horizon,
):
    """Return held-spread z using the entry-time model and fixed z baseline."""

    required = {"TARGET_RETURN", "PEER_RETURN"}
    if not required.issubset(features.columns):
        return pd.Series(np.nan, index=features.index, dtype=float)
    hedge_alpha = float(hedge_alpha)
    hedge_beta = float(hedge_beta)
    residual_z_mean = float(residual_z_mean)
    residual_z_std = float(residual_z_std)
    if (
        not all(
            np.isfinite(value)
            for value in (
                hedge_alpha,
                hedge_beta,
                residual_z_mean,
                residual_z_std,
            )
        )
        or residual_z_std <= 0
        or int(residual_horizon) < 1
    ):
        return pd.Series(np.nan, index=features.index, dtype=float)
    fixed_residual = features["TARGET_RETURN"] - (
        hedge_alpha + hedge_beta * features["PEER_RETURN"]
    )
    fixed_cumulative = fixed_residual.rolling(
        int(residual_horizon),
        min_periods=int(residual_horizon),
    ).sum()
    return (fixed_cumulative - residual_z_mean) / residual_z_std


def _open_price(prices, date):
    if date not in prices.index:
        return None
    value = float(prices.loc[date, "Open"])
    return value if np.isfinite(value) and value > 0 else None


def _close_price_as_of(prices, date):
    eligible = prices.loc[:date, "Close"].dropna()
    if eligible.empty:
        return None
    value = float(eligible.iloc[-1])
    return value if np.isfinite(value) and value > 0 else None


def _exit_price(prices, date, execution):
    if execution == NEXT_OPEN:
        return _open_price(prices, date)
    if execution == EVALUATION_END_MARK:
        return _close_price_as_of(prices, date)
    raise ValueError(f"Unsupported exit execution: {execution}")


def _missing_exit_legs(position, normalized_price_map, exit_date, execution):
    missing = []
    for leg in position["legs"]:
        prices = normalized_price_map.get(leg["ticker"])
        price = (
            _exit_price(prices, exit_date, execution)
            if prices is not None
            else None
        )
        if price is None:
            missing.append(leg["ticker"])
    return missing


def _execute_entry(leg_templates, normalized_price_map, entry_date):
    legs = []
    for template in leg_templates:
        prices = normalized_price_map.get(template["ticker"])
        price = _open_price(prices, entry_date) if prices is not None else None
        if price is None:
            return None
        leg = dict(template)
        leg["entry_date"] = entry_date.date().isoformat()
        leg["entry_price"] = price
        legs.append(leg)
    return legs


def _close_trade(
    position,
    normalized_price_map,
    exit_signal_date,
    exit_date,
    reason,
    execution=NEXT_OPEN,
    execution_status="FILLED_NEXT_OPEN",
):
    closed_legs = []
    pnl_ratio = 0.0
    for open_leg in position["legs"]:
        prices = normalized_price_map.get(open_leg["ticker"])
        exit_price = (
            _exit_price(prices, exit_date, execution)
            if prices is not None
            else None
        )
        if exit_price is None:
            return None
        leg = dict(open_leg)
        leg["exit_signal_date"] = exit_signal_date.date().isoformat()
        leg["exit_date"] = exit_date.date().isoformat()
        leg["exit_price"] = exit_price
        leg["exit_execution"] = execution
        raw_return = exit_price / leg["entry_price"] - 1.0
        leg_pnl_ratio = leg["signed_ratio"] * raw_return
        leg["underlying_return_pct"] = float(raw_return * 100.0)
        leg["pnl_on_target_notional_pct"] = float(leg_pnl_ratio * 100.0)
        pnl_ratio += leg_pnl_ratio
        closed_legs.append(leg)

    trade = {
        key: value
        for key, value in position.items()
        if key != "entry_idx" and not str(key).startswith("_")
    }
    trade["legs"] = closed_legs
    trade["exit_signal_date"] = exit_signal_date.date().isoformat()
    trade["exit_date"] = exit_date.date().isoformat()
    trade["exit_reason"] = reason
    trade["exit_execution"] = execution
    trade["exit_execution_status"] = execution_status
    trade["pnl_on_target_notional_pct"] = float(pnl_ratio * 100.0)
    trade["return_pct"] = float(
        pnl_ratio / trade["gross_ratio"] * 100.0
    )
    trade["return"] = trade["return_pct"]
    trade["exit_events"] = [
        {
            "date": trade["exit_date"],
            "signal_date": trade["exit_signal_date"],
            "reason": reason,
            "weight": 1.0,
            "execution": execution,
            "prices": {
                leg["ticker"]: leg["exit_price"] for leg in closed_legs
            },
        }
    ]
    return trade


def backtest_residual_stat_arb(
    ticker,
    price_map,
    metadata_by_ticker,
    side=None,
    evaluation_start=None,
    evaluation_end=None,
    config=None,
    cache_context=None,
):
    """Backtest actual target-plus-peer legs with next-open execution."""

    config = _config_or_default(config)
    normalized = normalize_price_map(price_map)
    full_target = normalized.get(ticker)
    if full_target is None or len(full_target) < 2:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "trades": [],
            "count": 0,
        }

    side = str(side).strip().upper() if side is not None else None
    if side not in {None, "LONG", "SHORT"}:
        raise ValueError("side must be LONG, SHORT, or None")
    evaluation_start = (
        pd.Timestamp(evaluation_start) if evaluation_start is not None else None
    )
    evaluation_end = (
        pd.Timestamp(evaluation_end) if evaluation_end is not None else None
    )
    target = (
        full_target.loc[:evaluation_end]
        if evaluation_end is not None
        else full_target
    )
    dates = target.index
    if len(dates) < 2:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "trades": [],
            "count": 0,
        }
    if evaluation_start is not None and not bool((dates >= evaluation_start).any()):
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "reason": "no target price exists in the evaluation window",
            "trades": [],
            "count": 0,
        }

    if evaluation_start is not None:
        peer_selection_dates = dates[dates < evaluation_start]
        if not len(peer_selection_dates):
            return {
                "status": "DATA_INSUFFICIENT",
                "strategy": STRATEGY_KEY,
                "ticker": ticker,
                "trades": [],
                "count": 0,
            }
        peer_selection_date = peer_selection_dates[-1]
    else:
        warmup_rows = max(
            config.train_window + config.residual_horizon,
            config.peer_correlation_lookback,
            config.z_window + config.residual_horizon,
        )
        if len(dates) <= warmup_rows + 1:
            return {
                "status": "DATA_INSUFFICIENT",
                "strategy": STRATEGY_KEY,
                "ticker": ticker,
                "trades": [],
                "count": 0,
            }
        peer_selection_date = dates[warmup_rows]
        evaluation_start = dates[warmup_rows + 1]

    ranked_peers = rank_peer_candidates(
        ticker,
        normalized,
        metadata_by_ticker,
        as_of=peer_selection_date,
        config=config,
        cache_context=cache_context,
    )
    selected_peers = [
        row["ticker"] for row in ranked_peers[: config.max_peers]
    ]
    if not selected_peers:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "trades": [],
            "count": 0,
        }
    features = build_residual_features(
        ticker,
        normalized,
        metadata_by_ticker,
        peers=selected_peers,
        as_of=dates[-1],
        config=config,
        cache_context=cache_context,
    )
    if features.empty:
        return {
            "status": "DATA_INSUFFICIENT",
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "trades": [],
            "count": 0,
        }

    trades = []
    position = None
    skipped_entries_missing_open = 0
    for index, signal_date in enumerate(dates[:-1]):
        if position is not None:
            if index < position["entry_idx"]:
                continue
            residual_z = (
                float(position["_held_residual_z"].loc[signal_date])
                if signal_date in position["_held_residual_z"].index
                and np.isfinite(position["_held_residual_z"].loc[signal_date])
                else np.nan
            )
            position["held_residual_z_latest"] = (
                residual_z if np.isfinite(residual_z) else None
            )
            holding_days = index - position["entry_idx"] + 1
            if position["side"] == "LONG":
                stopped = np.isfinite(residual_z) and residual_z <= -config.residual_stop_z
                normalized_residual = np.isfinite(residual_z) and residual_z >= 0.0
            else:
                stopped = np.isfinite(residual_z) and residual_z >= config.residual_stop_z
                normalized_residual = np.isfinite(residual_z) and residual_z <= 0.0

            reason = position.get("_pending_exit_reason")
            exit_signal_date = position.get(
                "_pending_exit_signal_date", signal_date
            )
            if reason is None:
                if stopped:
                    reason = "residual stop"
                elif normalized_residual:
                    reason = "residual zero"
                elif holding_days >= config.max_holding_days:
                    reason = f"{config.max_holding_days}d time exit"
                exit_signal_date = signal_date
            if reason is not None:
                exit_date = dates[index + 1]
                failures = position.setdefault("exit_execution_failures", [])
                missing_legs = _missing_exit_legs(
                    position,
                    normalized,
                    exit_date,
                    NEXT_OPEN,
                )
                if missing_legs:
                    failures.append(
                        {
                            "signal_date": exit_signal_date.date().isoformat(),
                            "intended_exit_date": exit_date.date().isoformat(),
                            "reason": reason,
                            "missing_legs": missing_legs,
                        }
                    )
                    position["_pending_exit_reason"] = reason
                    position["_pending_exit_signal_date"] = exit_signal_date
                    continue
                execution_status = (
                    "FILLED_DELAYED_AFTER_MISSING_OPEN"
                    if failures
                    else "FILLED_NEXT_OPEN"
                )
                closed = _close_trade(
                    position,
                    normalized,
                    exit_signal_date,
                    exit_date,
                    reason,
                    execution=NEXT_OPEN,
                    execution_status=execution_status,
                )
                if closed is not None:
                    trades.append(closed)
                    position = None
                # Do not open a replacement from the same close as an exit.
                continue
            continue

        entry_date = dates[index + 1]
        if evaluation_start is not None and entry_date < evaluation_start:
            continue
        if signal_date not in features.index:
            continue
        latest = features.loc[signal_date]
        if bool(latest["LONG_SIGNAL"]):
            signal_side = "LONG"
        elif bool(latest["SHORT_SIGNAL"]):
            signal_side = "SHORT"
        else:
            continue
        if side is not None and signal_side != side:
            continue
        hedge_beta = float(latest["HEDGE_BETA"])
        if not np.isfinite(hedge_beta):
            continue
        leg_templates = build_trade_legs(
            ticker,
            selected_peers,
            signal_side,
            hedge_beta,
        )
        legs = _execute_entry(leg_templates, normalized, entry_date)
        if legs is None:
            # A simultaneous hedge cannot be established at the target's
            # next open when any selected peer lacks that same-session open.
            skipped_entries_missing_open += 1
            continue
        ratio_summary = summarize_leg_ratios(legs)
        target_leg = next(leg for leg in legs if leg["role"] == "TARGET")
        hedge_alpha = float(latest["HEDGE_ALPHA"])
        residual_z_mean = float(latest["RESIDUAL_Z_MEAN"])
        residual_z_std = float(latest["RESIDUAL_Z_STD"])
        if (
            not np.isfinite(hedge_alpha)
            or not np.isfinite(residual_z_mean)
            or not np.isfinite(residual_z_std)
            or residual_z_std <= 0
        ):
            continue
        held_residual_z = build_fixed_hedge_residual_z(
            features,
            hedge_alpha,
            hedge_beta,
            residual_z_mean,
            residual_z_std,
            config.residual_horizon,
        )
        position = {
            "strategy": STRATEGY_KEY,
            "ticker": ticker,
            "side": signal_side,
            "signal_date": signal_date.date().isoformat(),
            "entry_date": entry_date.date().isoformat(),
            "entry_price": target_leg["entry_price"],
            "entry_execution": NEXT_OPEN,
            "signal_score": float(latest["SIGNAL_SCORE"]),
            "residual_z_at_signal": float(latest["RESIDUAL_Z"]),
            "previous_residual_z": float(latest["PREVIOUS_RESIDUAL_Z"]),
            "beta_market": float(latest["BETA_MARKET"]),
            "beta_sector_excess": float(latest["BETA_SECTOR_EXCESS"]),
            "hedge_alpha": hedge_alpha,
            "hedge_beta": hedge_beta,
            "residual_z_mean": residual_z_mean,
            "residual_z_std": residual_z_std,
            "selected_peers": list(selected_peers),
            "peer_correlations": [dict(row) for row in ranked_peers[: config.max_peers]],
            "peer_selection_date": peer_selection_date.date().isoformat(),
            "legs": legs,
            **ratio_summary,
            "entry_idx": index + 1,
            "exit_execution_failures": [],
            "_held_residual_z": held_residual_z,
        }

    if position is not None:
        mark_date = dates[-1]
        pending_reason = position.get("_pending_exit_reason")
        mark_reason = (
            f"evaluation-end mark after {pending_reason}"
            if pending_reason
            else "evaluation-end mark"
        )
        missing_marks = _missing_exit_legs(
            position,
            normalized,
            mark_date,
            EVALUATION_END_MARK,
        )
        if not missing_marks:
            marked = _close_trade(
                position,
                normalized,
                mark_date,
                mark_date,
                mark_reason,
                execution=EVALUATION_END_MARK,
                execution_status="MARKED_AT_EVALUATION_END",
            )
            if marked is not None:
                trades.append(marked)
                position = None

    closed_returns = [trade["return_pct"] for trade in trades]
    average_return = (
        float(np.mean(closed_returns)) if closed_returns else 0.0
    )
    win_rate = (
        float(np.mean(np.asarray(closed_returns) > 0) * 100.0)
        if closed_returns
        else 0.0
    )
    open_trade = None
    if position is not None:
        open_trade = {
            key: value
            for key, value in position.items()
            if key != "entry_idx" and not str(key).startswith("_")
        }
    eligible_dates = dates[dates >= evaluation_start]
    period_start = eligible_dates[0] if len(eligible_dates) else dates[-1]
    period = f"{period_start.date()} to {dates[-1].date()}"
    return {
        "status": "OK",
        "strategy": STRATEGY_KEY,
        "ticker": ticker,
        "period": period,
        "average_return_pct": average_return,
        "win_rate_pct": win_rate,
        "count": len(trades),
        "trades": trades,
        "open_trade": open_trade,
        "exit_execution_failures": sum(
            len(trade.get("exit_execution_failures") or [])
            for trade in trades
        )
        + (
            len(open_trade.get("exit_execution_failures") or [])
            if open_trade
            else 0
        ),
        "evaluation_end_marks": sum(
            trade.get("exit_execution_status") == "MARKED_AT_EVALUATION_END"
            for trade in trades
        ),
        "skipped_entries_missing_open": skipped_entries_missing_open,
    }


def run_residual_stat_arb_backtest(
    ticker,
    price_map,
    metadata_by_ticker,
    include_trades=False,
    **kwargs,
):
    """Compatibility tuple wrapper for the existing universe aggregator."""

    result = backtest_residual_stat_arb(
        ticker,
        price_map,
        metadata_by_ticker,
        **kwargs,
    )
    summary = (
        result.get("average_return_pct", 0.0),
        result.get("win_rate_pct", 0.0),
        result.get("count", 0),
        result.get("period", "N/A"),
        result.get("trades", []),
    )
    return summary if include_trades else summary[:4]
