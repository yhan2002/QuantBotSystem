"""Daily relative-equilibrium reversion with an explicit factor hedge."""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

import core_backtest_periods
import core_cross_sectional_neutralization
import core_entry_execution
import core_market_data_store
import core_position_mode
import core_signal_scoring
import core_universe_backtest
from core_portfolio_allocator import PortfolioPolicy
from core_signal_model import Signal


STRATEGY_KEY = "RELATIVE_EQUILIBRIUM"
MARKET_TICKER = "ACWI"


@dataclass(frozen=True)
class RelativeEquilibriumConfig:
    regression_window: int = 60
    min_observations: int = 50
    turnover_lookback: int = 20
    max_turnover_ratio: float = 2.0
    max_sector_peers: int = 5
    min_sector_peers: int = 3
    entry_s_score: float = -1.25
    exit_s_score: float = -0.50
    stop_s_score: float = -3.00
    min_half_life: float = 3.0
    max_half_life: float = 30.0
    max_abs_beta: float = 3.0
    max_hedge_gross: float = 2.0


DEFAULT_CONFIG = RelativeEquilibriumConfig()


def _normalize_frame(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    required = {"Open", "Close", "Volume"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    clean = frame.copy()
    clean.index = pd.to_datetime(clean.index, errors="coerce")
    clean = clean.loc[~clean.index.isna()]
    if clean.index.tz is not None:
        clean.index = clean.index.tz_localize(None)
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    for column in required | ({"Adj Close"} & set(clean.columns)):
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    if "Adj Close" not in clean:
        clean["Adj Close"] = clean["Close"]
    return clean.dropna(subset=["Open", "Close", "Adj Close", "Volume"])


def _frame(price_map, ticker):
    return _normalize_frame((price_map or {}).get(ticker))


def _as_of(frame, value=None):
    if frame.empty:
        return None
    if value is None:
        return frame.index[-1]
    cutoff = pd.Timestamp(value)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_localize(None)
    dates = frame.index[frame.index <= cutoff]
    return dates[-1] if len(dates) else None


def _metadata_key(metadata_by_ticker, ticker):
    row = (metadata_by_ticker or {}).get(ticker, {})
    return (
        str(row.get("listing_country") or "").strip().upper(),
        str(row.get("sector") or "").strip().upper(),
    )


def _select_sector_peers(
    ticker,
    price_map,
    metadata_by_ticker,
    selection_date,
    config=DEFAULT_CONFIG,
):
    group = _metadata_key(metadata_by_ticker, ticker)
    if not all(group):
        return []
    ranked = []
    for candidate in sorted(price_map):
        if candidate in (ticker, MARKET_TICKER):
            continue
        if _metadata_key(metadata_by_ticker, candidate) != group:
            continue
        prices = _frame(price_map, candidate).loc[:selection_date]
        turnover = (prices["Close"] * prices["Volume"]).tail(
            config.regression_window
        ).dropna()
        if len(turnover) < config.turnover_lookback:
            continue
        ranked.append((float(turnover.mean()), candidate))
    peers = [ticker for _, ticker in sorted(ranked, key=lambda row: (-row[0], row[1]))]
    return peers[: config.max_sector_peers]


def _lagged_market_returns(market_returns, target_index, listing_country):
    if str(listing_country or "").upper() == "US":
        return market_returns.reindex(target_index)
    left = pd.DataFrame({"date": target_index})
    right = market_returns.dropna().rename("value").reset_index()
    right.columns = ["date", "value"]
    if right.empty:
        return pd.Series(np.nan, index=target_index, dtype=float)
    aligned = pd.merge_asof(
        left.sort_values("date"),
        right.sort_values("date"),
        on="date",
        direction="backward",
        allow_exact_matches=False,
    )
    return pd.Series(aligned["value"].to_numpy(), index=target_index, dtype=float)


def _factor_frame(
    ticker,
    price_map,
    metadata_by_ticker,
    peers,
    as_of=None,
    config=DEFAULT_CONFIG,
):
    target = _frame(price_map, ticker)
    market = _frame(price_map, MARKET_TICKER)
    signal_date = _as_of(target, as_of)
    if signal_date is None or market.empty or len(peers) < config.min_sector_peers:
        return pd.DataFrame()
    target = target.loc[:signal_date]
    target_returns = target["Adj Close"].pct_change(fill_method=None)
    market_returns = market["Adj Close"].pct_change(fill_method=None)
    listing_country = _metadata_key(metadata_by_ticker, ticker)[0]
    aligned_market = _lagged_market_returns(
        market_returns, target.index, listing_country
    )
    peer_returns = []
    for peer in peers:
        prices = _frame(price_map, peer)
        peer_returns.append(
            prices["Adj Close"].pct_change(fill_method=None).reindex(target.index)
        )
    sector_return = pd.concat(peer_returns, axis=1).mean(axis=1, skipna=False)
    factors = pd.DataFrame(
        {
            "TARGET_RETURN": target_returns,
            "MARKET_RETURN": aligned_market,
            "SECTOR_RETURN": sector_return,
        },
        index=target.index,
    )
    factors["SECTOR_EXCESS_RETURN"] = (
        factors["SECTOR_RETURN"] - factors["MARKET_RETURN"]
    )
    turnover = target["Close"] * target["Volume"]
    prior_mean = turnover.shift(1).rolling(config.turnover_lookback).mean()
    factors["TURNOVER_RATIO"] = turnover / prior_mean.replace(0.0, np.nan)
    return factors.replace([np.inf, -np.inf], np.nan)


def _rolling_models(factors, config=DEFAULT_CONFIG):
    result = factors.copy()
    columns = (
        "ALPHA",
        "BETA_MARKET",
        "BETA_SECTOR",
        "HEDGE_BETA",
        "RHO",
        "HALF_LIFE",
        "OU_MEAN",
        "OU_SIGMA",
        "RESIDUAL_VOL",
        "SPREAD_LEVEL",
        "S_SCORE",
        "HEDGE_GROSS",
        "RAW_RESIDUAL",
    )
    for column in columns:
        result[column] = np.nan
    result["MODEL_READY"] = False
    result["MODEL_VALID"] = False
    required = [
        "TARGET_RETURN",
        "MARKET_RETURN",
        "SECTOR_RETURN",
        "SECTOR_EXCESS_RETURN",
    ]

    for position in range(len(result)):
        training = result.iloc[
            max(0, position - config.regression_window):position
        ][required].dropna()
        current = result.iloc[position][required]
        if len(training) < config.min_observations or current.isna().any():
            continue
        y = training["TARGET_RETURN"].to_numpy(dtype=float)
        x = training[["MARKET_RETURN", "SECTOR_EXCESS_RETURN"]].to_numpy(dtype=float)
        design = np.column_stack([np.ones(len(training)), x])
        alpha, beta_market, beta_sector = np.linalg.lstsq(
            design, y, rcond=None
        )[0]
        current_residual = float(current["TARGET_RETURN"]) - (
            float(alpha)
            + float(beta_market) * float(current["MARKET_RETURN"])
            + float(beta_sector) * float(current["SECTOR_EXCESS_RETURN"])
        )
        sector_returns = training["SECTOR_RETURN"].to_numpy(dtype=float)
        centered_sector = sector_returns - sector_returns.mean()
        sector_denominator = float(np.dot(centered_sector, centered_sector))
        hedge_beta = (
            float(np.dot(centered_sector, y - y.mean()) / sector_denominator)
            if sector_denominator > np.finfo(float).eps
            else float("nan")
        )
        for column, value in {
            "ALPHA": alpha,
            "BETA_MARKET": beta_market,
            "BETA_SECTOR": beta_sector,
            "HEDGE_BETA": hedge_beta,
            "RAW_RESIDUAL": current_residual,
        }.items():
            result.iloc[position, result.columns.get_loc(column)] = float(value)
        residual_history = result.iloc[
            max(0, position - config.regression_window):position
        ]["RAW_RESIDUAL"].dropna().to_numpy(dtype=float)
        if len(residual_history) < config.min_observations:
            continue
        spread = np.cumsum(residual_history)
        ar_design = np.column_stack([np.ones(len(spread) - 1), spread[:-1]])
        ar_intercept, rho = np.linalg.lstsq(
            ar_design, spread[1:], rcond=None
        )[0]
        innovation = spread[1:] - ar_design @ np.array([ar_intercept, rho])
        residual_vol = float(np.std(residual_history, ddof=1))
        result.iloc[position, result.columns.get_loc("MODEL_READY")] = True
        if not 0.0 < rho < 1.0:
            continue
        half_life = -math.log(2.0) / math.log(float(rho))
        innovation_vol = float(np.std(innovation, ddof=1))
        denominator = 1.0 - float(rho) ** 2
        if innovation_vol <= 0.0 or denominator <= 0.0 or residual_vol <= 0.0:
            continue
        ou_mean = float(ar_intercept) / (1.0 - float(rho))
        ou_sigma = innovation_vol / math.sqrt(denominator)
        spread_level = float(spread[-1]) + current_residual
        s_score = (spread_level - ou_mean) / ou_sigma
        hedge_gross = abs(hedge_beta)
        values = {
            "ALPHA": float(alpha),
            "BETA_MARKET": float(beta_market),
            "BETA_SECTOR": float(beta_sector),
            "HEDGE_BETA": hedge_beta,
            "RHO": float(rho),
            "HALF_LIFE": half_life,
            "OU_MEAN": ou_mean,
            "OU_SIGMA": ou_sigma,
            "RESIDUAL_VOL": residual_vol,
            "SPREAD_LEVEL": spread_level,
            "S_SCORE": s_score,
            "HEDGE_GROSS": hedge_gross,
        }
        for column, value in values.items():
            result.iloc[position, result.columns.get_loc(column)] = value
        result.iloc[position, result.columns.get_loc("MODEL_VALID")] = bool(
            config.min_half_life <= half_life <= config.max_half_life
            and hedge_beta > 0.0
            and abs(beta_market) <= config.max_abs_beta
            and abs(beta_sector) <= config.max_abs_beta
            and abs(hedge_beta) <= config.max_abs_beta
            and hedge_gross <= config.max_hedge_gross
        )
    return result


def build_relative_equilibrium_features(
    ticker,
    price_map,
    metadata_by_ticker,
    *,
    as_of=None,
    peer_selection_as_of=None,
    config=DEFAULT_CONFIG,
):
    target = _frame(price_map, ticker)
    signal_date = _as_of(target, as_of)
    if signal_date is None or ticker == MARKET_TICKER:
        return pd.DataFrame(), []
    selection_date = _as_of(
        target,
        signal_date if peer_selection_as_of is None else peer_selection_as_of,
    )
    if selection_date is None or selection_date > signal_date:
        return pd.DataFrame(), []
    peers = _select_sector_peers(
        ticker, price_map, metadata_by_ticker, selection_date, config
    )
    factors = _factor_frame(
        ticker,
        price_map,
        metadata_by_ticker,
        peers,
        as_of=signal_date,
        config=config,
    )
    return (_rolling_models(factors, config) if not factors.empty else factors), peers


def _trade_legs(ticker, peers, row):
    hedge_beta = float(row["HEDGE_BETA"])
    legs = [{"ticker": ticker, "signed_ratio": 1.0, "role": "ALPHA"}]
    peer_ratio = -hedge_beta / len(peers)
    legs.extend(
        {"ticker": peer, "signed_ratio": peer_ratio, "role": "SECTOR_HEDGE"}
        for peer in peers
    )
    return legs


def analyze_relative_equilibrium(
    ticker,
    price_map,
    metadata_by_ticker,
    *,
    as_of=None,
    peer_selection_as_of=None,
    config=DEFAULT_CONFIG,
):
    features, peers = build_relative_equilibrium_features(
        ticker,
        price_map,
        metadata_by_ticker,
        as_of=as_of,
        peer_selection_as_of=peer_selection_as_of,
        config=config,
    )
    if features.empty:
        return {"status": "DATA_INSUFFICIENT", "score": 0.0, "ticker": ticker}
    latest = features.iloc[-1]
    base = {
        "ticker": ticker,
        "signal_date": features.index[-1].date().isoformat(),
        "peers": peers,
        "score": 0.0,
    }
    if not bool(latest["MODEL_READY"]):
        return {**base, "status": "DATA_INSUFFICIENT"}
    if not bool(latest["MODEL_VALID"]):
        return {**base, "status": "NO_SIGNAL", "reason": "relationship gate"}
    s_score = float(latest["S_SCORE"])
    turnover_ratio = float(latest["TURNOVER_RATIO"])
    base.update(
        {
            "s_score": s_score,
            "half_life": float(latest["HALF_LIFE"]),
            "turnover_ratio": turnover_ratio,
            "hedge_gross": float(latest["HEDGE_GROSS"]),
        }
    )
    if not math.isfinite(turnover_ratio) or turnover_ratio >= config.max_turnover_ratio:
        return {**base, "status": "NO_SIGNAL", "reason": "turnover veto"}
    if s_score > config.entry_s_score:
        return {**base, "status": "NO_SIGNAL", "reason": "s-score not extreme"}
    score = -s_score / float(latest["RESIDUAL_VOL"])
    return {
        **base,
        "status": "OK",
        "side": "LONG",
        "score": score,
        "legs": _trade_legs(ticker, peers, latest),
    }


def _prices_on(price_map, legs, date, column):
    prices = {}
    for leg in legs:
        frame = _frame(price_map, leg["ticker"])
        if date not in frame.index:
            return None
        value = float(frame.at[date, column])
        if not math.isfinite(value) or value <= 0.0:
            return None
        prices[leg["ticker"]] = value
    return prices


def _finish_trade(position, date, reason, execution, leg_prices):
    weighted_return = sum(
        float(leg["signed_ratio"])
        * (leg_prices[leg["ticker"]] / float(leg["entry_price"]) - 1.0)
        for leg in position["legs"]
    ) * 100.0
    return {
        key: value
        for key, value in position.items()
        if key
        not in {
            "entry_idx",
            "pending_exit",
            "spread_level",
            "alpha",
            "beta_market",
            "beta_sector",
            "ou_mean",
            "ou_sigma",
            "max_holding_sessions",
        }
    } | {
        "exit_events": [
            {
                "date": date.date().isoformat(),
                "reason": reason,
                "weight": 1.0,
                "execution": execution,
                "leg_prices": leg_prices,
            }
        ],
        "return": weighted_return,
    }


def run_relative_equilibrium_backtest(
    ticker,
    side=None,
    period_days=1200,
    include_trades=False,
    df=None,
    price_map=None,
    metadata_by_ticker=None,
    evaluation_days=None,
    evaluation_start=None,
    evaluation_end=None,
    entry_execution=core_entry_execution.NEXT_OPEN,
    adverse_gap_limit_pct=core_entry_execution.DEFAULT_ADVERSE_GAP_LIMIT_PCT,
    fixed_exit_sessions=None,
    config=DEFAULT_CONFIG,
):
    fixed_exit_sessions = core_entry_execution.normalize_fixed_exit_sessions(
        fixed_exit_sessions
    )
    entry_execution = core_entry_execution.normalize_mode(entry_execution)
    if entry_execution != core_entry_execution.NEXT_OPEN:
        raise ValueError("Relative Equilibrium supports NEXT_OPEN entry only")
    allow_long_entries = side in (None, "LONG")
    if price_map is None:
        price_map = {
            ticker: core_market_data_store.get_price_data(ticker, period_days),
            MARKET_TICKER: core_market_data_store.get_price_data(
                MARKET_TICKER, period_days
            ),
        }
    else:
        price_map = dict(price_map)
    if df is not None:
        price_map[ticker] = df
    metadata_by_ticker = metadata_by_ticker or core_market_data_store.get_security_metadata(
        price_map
    )
    target = _frame(price_map, ticker)
    if evaluation_end is not None:
        target = target.loc[:pd.Timestamp(evaluation_end)]
        price_map[ticker] = target
    if target.empty:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]

    eligible_dates = target.index
    if evaluation_start is not None:
        eligible_dates = eligible_dates[eligible_dates >= pd.Timestamp(evaluation_start)]
    if evaluation_days is not None:
        eligible_dates = eligible_dates[-int(evaluation_days):]
    if not len(eligible_dates):
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]
    evaluation_start_date = eligible_dates[0]
    selection_candidates = target.index[target.index < evaluation_start_date]
    selection_date = selection_candidates[-1] if len(selection_candidates) else None
    if selection_date is None:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]
    features, peers = build_relative_equilibrium_features(
        ticker,
        price_map,
        metadata_by_ticker,
        as_of=eligible_dates[-1],
        peer_selection_as_of=selection_date,
        config=config,
    )
    period_label = f"{eligible_dates[0].date()} to {eligible_dates[-1].date()}"
    if features.empty or len(peers) < config.min_sector_peers:
        result = (0.0, 0.0, 0, period_label, [])
        return result if include_trades else result[:4]

    trades = []
    position = None
    start_idx = target.index.get_loc(eligible_dates[0])
    end_idx = target.index.get_loc(eligible_dates[-1])
    for idx in range(start_idx, end_idx + 1):
        date = target.index[idx]
        if position is not None and position.get("pending_exit"):
            leg_prices = _prices_on(price_map, position["legs"], date, "Open")
            if leg_prices is not None:
                trades.append(
                    _finish_trade(
                        position,
                        date,
                        position["pending_exit"],
                        core_entry_execution.NEXT_OPEN,
                        leg_prices,
                    )
                )
                position = None
            else:
                continue

        if position is not None:
            if fixed_exit_sessions is not None:
                if idx - position["entry_idx"] >= fixed_exit_sessions:
                    leg_prices = _prices_on(price_map, position["legs"], date, "Close")
                    if leg_prices is not None:
                        trades.append(
                            _finish_trade(
                                position,
                                date,
                                f"D+{fixed_exit_sessions} fixed close",
                                "CLOSE",
                                leg_prices,
                            )
                        )
                        position = None
                continue
            if idx < position["entry_idx"] or date not in features.index:
                continue
            row = features.loc[date]
            if row[["TARGET_RETURN", "MARKET_RETURN", "SECTOR_EXCESS_RETURN"]].isna().any():
                position["pending_exit"] = "relationship break"
                continue
            frozen_residual = float(row["TARGET_RETURN"]) - (
                position["alpha"]
                + position["beta_market"] * float(row["MARKET_RETURN"])
                + position["beta_sector"] * float(row["SECTOR_EXCESS_RETURN"])
            )
            position["spread_level"] += frozen_residual
            held_s = (
                position["spread_level"] - position["ou_mean"]
            ) / position["ou_sigma"]
            held_sessions = idx - position["entry_idx"] + 1
            if not bool(row["MODEL_VALID"]):
                position["pending_exit"] = "relationship break"
            elif held_s >= config.exit_s_score:
                position["pending_exit"] = "equilibrium convergence"
            elif held_s <= config.stop_s_score:
                position["pending_exit"] = "residual hard stop"
            elif held_sessions >= position["max_holding_sessions"]:
                position["pending_exit"] = "2x half-life time exit"
            continue

        if not allow_long_entries or idx >= end_idx or date not in features.index:
            continue
        row = features.loc[date]
        turnover_ratio = float(row["TURNOVER_RATIO"])
        if (
            not bool(row["MODEL_VALID"])
            or not math.isfinite(turnover_ratio)
            or turnover_ratio >= config.max_turnover_ratio
            or float(row["S_SCORE"]) > config.entry_s_score
        ):
            continue
        execution = core_entry_execution.resolve_entry(
            target,
            idx,
            "LONG",
            mode=entry_execution,
            adverse_gap_limit_pct=adverse_gap_limit_pct,
        )
        if execution is None or execution["entry_idx"] > end_idx:
            continue
        entry_date = target.index[execution["entry_idx"]]
        legs = _trade_legs(ticker, peers, row)
        entry_prices = _prices_on(price_map, legs, entry_date, "Open")
        if entry_prices is None:
            continue
        for leg in legs:
            leg["entry_price"] = entry_prices[leg["ticker"]]
        position = {
            "strategy": STRATEGY_KEY,
            "side": "LONG",
            "signal_date": date.date().isoformat(),
            "entry_date": entry_date.date().isoformat(),
            "entry_price": entry_prices[ticker],
            "signal_score": -float(row["S_SCORE"]) / float(row["RESIDUAL_VOL"]),
            "setup": "OU_S_SCORE_LONG",
            "structure": "STATIONARY_FACTOR_RESIDUAL",
            "status": "OK",
            "entry_execution": execution["mode"],
            "signal_close": execution["signal_close"],
            "next_open": execution["next_open"],
            "signal_to_open_return_pct": execution["signal_to_open_return_pct"],
            "signal_to_fill_return_pct": execution["signal_to_fill_return_pct"],
            "metadata": {
                "peers": peers,
                "entry_s_score": float(row["S_SCORE"]),
                "entry_half_life": float(row["HALF_LIFE"]),
                "turnover_ratio": turnover_ratio,
                "market_beta": float(row["BETA_MARKET"]),
                "sector_beta": float(row["BETA_SECTOR"]),
                "hedge_beta": float(row["HEDGE_BETA"]),
            },
            "legs": legs,
            "entry_idx": execution["entry_idx"],
            "spread_level": float(row["SPREAD_LEVEL"]),
            "alpha": float(row["ALPHA"]),
            "beta_market": float(row["BETA_MARKET"]),
            "beta_sector": float(row["BETA_SECTOR"]),
            "ou_mean": float(row["OU_MEAN"]),
            "ou_sigma": float(row["OU_SIGMA"]),
            "max_holding_sessions": min(
                30, max(1, math.ceil(2.0 * float(row["HALF_LIFE"])))
            ),
        }

    if fixed_exit_sessions is None and position is not None:
        final_date = eligible_dates[-1]
        leg_prices = _prices_on(price_map, position["legs"], final_date, "Close")
        if leg_prices is not None:
            trades.append(
                _finish_trade(
                    position,
                    final_date,
                    "evaluation end mark",
                    "CLOSE",
                    leg_prices,
                )
            )
    values = [float(trade["return"]) for trade in trades]
    result = (
        sum(values) / len(values) if values else 0.0,
        sum(value > 0.0 for value in values) / len(values) * 100.0 if values else 0.0,
        len(values),
        period_label,
        trades,
    )
    return result if include_trades else result[:4]


def _notify(progress_callback, stage, completed, total, ticker, succeeded):
    if progress_callback is None:
        return
    try:
        progress_callback(stage, completed, total, ticker, succeeded)
    except Exception:
        pass


def _stage_callback(progress_callback, stage):
    if progress_callback is None:
        return None
    return lambda completed, total, ticker, succeeded: _notify(
        progress_callback, stage, completed, total, ticker, succeeded
    )


def _format_candidates(rows):
    if not rows:
        return "None\n"
    return "\n".join(
        f"{idx}. {row['ticker']:<8} | LONG | s {row['s_score']:+.2f} | "
        f"half-life {row['half_life']:.1f}d | turnover {row['turnover_ratio']:.1f}x | "
        f"hedge gross {row['hedge_gross']:.2f}x | alpha weight {row['requested_weight'] * 100:.1f}%"
        for idx, row in enumerate(rows, start=1)
    ) + "\n"


def scan_relative_equilibrium(
    universe_list,
    neutralization="NONE",
    position_mode=core_position_mode.LONG_SHORT_NEUTRAL,
    progress_callback=None,
    report_artifacts=None,
    fixed_exit_sessions=None,
    price_map=None,
    metadata_by_ticker=None,
    config=DEFAULT_CONFIG,
):
    universe_list = list(dict.fromkeys(universe_list))
    if price_map is None:
        price_map = {
            ticker: core_market_data_store.get_price_data(ticker, period_days=1200)
            for ticker in universe_list + [MARKET_TICKER]
        }
    metadata_by_ticker = metadata_by_ticker or core_market_data_store.get_security_metadata(
        universe_list
    )
    scored, failed, raw_scores = [], [], {}
    for completed, ticker in enumerate(universe_list, start=1):
        succeeded = False
        try:
            row = analyze_relative_equilibrium(
                ticker, price_map, metadata_by_ticker, config=config
            )
            succeeded = row["status"] != "DATA_INSUFFICIENT"
            if row["status"] == "OK":
                scored.append(row)
                raw_scores[ticker] = row["score"]
            elif row["status"] == "DATA_INSUFFICIENT":
                failed.append(ticker)
            else:
                raw_scores[ticker] = 0.0
        finally:
            _notify(
                progress_callback,
                "CURRENT_SCAN",
                completed,
                len(universe_list),
                ticker,
                succeeded,
            )

    neutralization = core_cross_sectional_neutralization.normalize_mode(neutralization)
    position_mode = core_position_mode.normalize_mode(position_mode)
    neutralized = core_cross_sectional_neutralization.neutralize_scores(
        raw_scores, neutralization, metadata_by_ticker
    )
    candidate_pairs = []
    if core_position_mode.allows_long(position_mode):
        for row in scored:
            row["neutralized_score"] = neutralized.get(row["ticker"], row["score"])
            candidate_pairs.append(
                (
                    Signal(
                        strategy=STRATEGY_KEY,
                        ticker=row["ticker"],
                        side="LONG",
                        signal_date=row["signal_date"],
                        raw_score=row["score"],
                        status=row["status"],
                        metadata={
                            "s_score": row["s_score"],
                            "half_life": row["half_life"],
                        },
                        entry_execution=core_entry_execution.NEXT_OPEN,
                    ),
                    row["neutralized_score"],
                )
            )
    scored_signals = {
        item.signal.ticker: item
        for item in core_signal_scoring.score_signal_cohort(candidate_pairs)
    }
    candidates = sorted(
        (
            {
                **row,
                "rank_score": scored_signals[row["ticker"]].rank_score,
                "requested_weight": scored_signals[row["ticker"]].requested_weight,
            }
            for row in scored
            if row["ticker"] in scored_signals
        ),
        key=lambda row: (row["rank_score"], row["score"]),
        reverse=True,
    )[:3]

    runner_kwargs = {
        "period_days": 1200,
        "neutralization": neutralization,
        "position_mode": core_position_mode.LONG_SHORT_NEUTRAL,
        "portfolio_accounting": True,
        "portfolio_policy": PortfolioPolicy(
            position_mode=core_position_mode.LONG_SHORT_NEUTRAL
        ),
        "side": core_position_mode.side_filter(position_mode),
        "fixed_exit_sessions": fixed_exit_sessions,
        "price_map": price_map,
        "metadata_by_ticker": metadata_by_ticker,
        "config": config,
    }
    selection_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_relative_equilibrium_backtest,
        progress_callback=_stage_callback(progress_callback, "IS_BACKTEST"),
        **runner_kwargs,
        **core_backtest_periods.selection_window(),
    )
    oos_result = core_universe_backtest.aggregate_universe_backtest(
        universe_list,
        run_relative_equilibrium_backtest,
        progress_callback=_stage_callback(progress_callback, "OOS_BACKTEST"),
        **runner_kwargs,
        **core_backtest_periods.oos_window(),
    )
    exit_text = (
        f"close at D+{fixed_exit_sessions}; native exits disabled; incomplete D+N holdings excluded"
        if fixed_exit_sessions is not None
        else "exit next open after s >= -0.50, s <= -3.00, relationship break, or 2x entry half-life (30d cap); otherwise evaluation-end mark"
    )
    report = (
        "**Relative Equilibrium Scan Report**\n"
        "====================================\n\n"
        f"{core_cross_sectional_neutralization.report_line(neutralization, raw_scores)}\n"
        f"{core_position_mode.report_line(position_mode)}\n"
        "Alpha direction: LONG only; hedge legs remain enabled for portfolio accounting.\n"
        "Frequency: daily close signal; next-session open execution (no intraday rule).\n\n"
        "**LONG Candidates**\n"
        "Rule: market + synthetic sector residual s <= -1.25, OU half-life 3-30d, turnover <2.0x.\n"
        f"{_format_candidates(candidates)}\n"
        "**SHORT Candidates**\n"
        "None (factor shorts are hedges, not alpha signals).\n"
        "Data note: ACWI is a signal-only control (lagged for non-US closes); the executable hedge "
        "uses up to five same-country sector names. "
        "Sector labels are current, and the local DB has no point-in-time earnings calendar.\n"
        f"{core_universe_backtest.format_fixed_is_oos_backtest(selection_result, oos_result, entry_text='60-session market-conditioned sector residual, OU half-life 3-30d, s <= -1.25, turnover <2.0x; enter alpha and same-country hedge next open', exit_text=exit_text, strategy_key='relative_equilibrium', report_artifacts=report_artifacts)}"
    )
    if failed:
        report += "\n----------------------------------------\nInsufficient local data: " + ", ".join(
            sorted(failed)
        )
    return report
