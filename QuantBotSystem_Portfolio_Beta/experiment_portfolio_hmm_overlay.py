"""Developer-only BASELINE/RULE/HMM regime-routing comparison.

The command reads the existing local cache and never downloads data.  It
combines Momentum, Box Range, and Reversion trades before applying the sizing
hook because the HMM's purpose is capital routing between strategies.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

import config
import core_backtest_periods
import core_market_data_store
import core_portfolio_allocator
import core_portfolio_performance
import experiment_portfolio_rl_overlay as common
import portfolio_hmm_overlay


STRATEGIES = ("Momentum", "Box Range", "Reversion")
ESTIMATED_WINDOWS = 2
ESTIMATED_VARIANTS = 3
HISTORY_DAYS = 1460


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _write_output(path, payload):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"experiment output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _market_close(frame):
    column = "Adj Close" if "Adj Close" in frame.columns else "Close"
    return pd.to_numeric(frame[column], errors="coerce")


def _universe_close(price_data, universe):
    columns = {}
    for ticker in universe:
        frame = price_data.get(ticker)
        if frame is None or frame.empty:
            continue
        column = "Adj Close" if "Adj Close" in frame.columns else "Close"
        if column in frame.columns:
            columns[ticker] = pd.to_numeric(frame[column], errors="coerce")
    if not columns:
        raise RuntimeError("local cache contains no universe close prices")
    return pd.concat(columns, axis=1).sort_index()


def _combined_trades(universe, price_data, window, window_name):
    trades = []
    failures = []
    for strategy in STRATEGIES:
        results = common._collect_ticker_results(
            strategy,
            universe,
            price_data,
            window,
        )
        failures.extend(
            {
                "window": window_name,
                "strategy": strategy,
                "ticker": ticker,
                "error_type": type(result).__name__,
                "error": str(result),
            }
            for ticker, result in results.items()
            if isinstance(result, Exception)
        )
        scored, _ = common._scored_trades(strategy, universe, results)
        trades.extend(scored)
    return [
        {**trade, "trade_id": f"{window_name}-{index:07d}"}
        for index, trade in enumerate(trades, start=1)
    ], failures


def _routing_metrics(performance):
    trades = [
        trade
        for trade in performance["ledger"]["trades"]
        if float(trade.get("base_requested_weight", 0.0)) > 0.0
    ]
    base_total = sum(float(trade["base_requested_weight"]) for trade in trades)
    requested_total = sum(float(trade["requested_weight"]) for trade in trades)
    turnover = sum(
        abs(
            float(trade["requested_weight"])
            - float(trade["base_requested_weight"])
        )
        for trade in trades
    )
    strategy_ratios = {}
    for strategy in STRATEGIES:
        selected = [trade for trade in trades if trade.get("strategy") == strategy]
        strategy_base = sum(
            float(trade["base_requested_weight"]) for trade in selected
        )
        strategy_ratios[strategy] = (
            sum(float(trade["requested_weight"]) for trade in selected)
            / strategy_base
            if strategy_base > 0.0
            else None
        )
    return {
        "redistribution_turnover_pct": (
            0.5 * turnover / base_total * 100.0 if base_total > 0.0 else None
        ),
        "capital_preservation_ratio": (
            requested_total / base_total if base_total > 0.0 else None
        ),
        "strategy_capital_ratios": strategy_ratios,
    }


def _state_statistics(probability, window):
    if probability is None:
        return None
    values = probability.loc[
        pd.Timestamp(window["evaluation_start"]) : pd.Timestamp(
            window["evaluation_end"]
        )
    ].dropna()
    if values.empty:
        return None
    states = [
        "TREND" if value > 0.5 else "NON_TREND" if value < 0.5 else "NEUTRAL"
        for value in values
    ]
    runs = []
    for state in states:
        if runs and runs[-1][0] == state:
            runs[-1][1] += 1
        else:
            runs.append([state, 1])
    durations = {
        state: [length for run_state, length in runs if run_state == state]
        for state in ("TREND", "NON_TREND", "NEUTRAL")
    }
    return {
        "observations": len(states),
        "transitions": max(0, len(runs) - 1),
        "mean_p_trend": float(values.mean()),
        "trend_fraction_pct": states.count("TREND") / len(states) * 100.0,
        "average_duration_sessions": {
            state: float(np.mean(lengths)) if lengths else None
            for state, lengths in durations.items()
        },
        "maximum_duration_sessions": {
            state: max(lengths) if lengths else None
            for state, lengths in durations.items()
        },
    }


def _summary(window_name, variant, performance, metrics, probability, window):
    strategy = performance["strategy"]
    market = performance["market_benchmark"]
    return {
        "window": window_name,
        "variant": variant,
        "roi_pct": strategy.get("roi_pct"),
        "cagr_pct": strategy.get("cagr_pct"),
        "mdd_pct": strategy.get("mdd_pct"),
        "sharpe": strategy.get("sharpe"),
        "acwi_roi_pct": market.get("roi_pct"),
        **_routing_metrics(performance),
        "cvar": metrics.get("cvar"),
        "average_gross_pct": metrics.get("average_gross_pct"),
        "state_persistence": _state_statistics(probability, window),
        "filled_trades": performance["ledger"].get("filled_trades"),
    }


def run_experiment(
    *,
    group="CSQ_UNIV",
    estimate_only=False,
    confirm_long_run=False,
    output=None,
):
    if group not in config.universe_groups:
        raise KeyError(f"Unknown universe group: {group}")
    universe = sorted(
        set(config.universe_groups[group])
        - {core_portfolio_performance.MARKET_BENCHMARK_TICKER}
    )
    estimate = {
        "group": group,
        "universe_count": len(universe),
        "local_history_days": HISTORY_DAYS,
        "strategy_scans": len(universe) * len(STRATEGIES) * ESTIMATED_WINDOWS,
        "portfolio_replays": ESTIMATED_WINDOWS * ESTIMATED_VARIANTS,
        "external_downloads": 0,
    }
    if estimate_only:
        return estimate
    if not confirm_long_run:
        raise RuntimeError(
            "long HMM comparison not confirmed; run --estimate-only first, "
            "then rerun with --confirm-long-run"
        )

    print(f"Loading local OHLCV: {len(universe)} tickers", flush=True)
    price_data = {
        ticker: core_market_data_store.get_price_data(ticker, period_days=HISTORY_DAYS)
        for ticker in universe
    }
    acwi_ticker = core_portfolio_performance.MARKET_BENCHMARK_TICKER
    acwi_frame = core_market_data_store.get_price_data(
        acwi_ticker,
        period_days=HISTORY_DAYS,
    )
    if acwi_frame is None or acwi_frame.empty:
        raise RuntimeError("Local ACWI data is required")
    coverage = sum(
        frame is not None and not frame.empty for frame in price_data.values()
    ) / max(1, len(universe))
    if coverage < common.MINIMUM_DATA_COVERAGE_RATIO:
        raise RuntimeError(
            "local universe data coverage is below "
            f"{common.MINIMUM_DATA_COVERAGE_RATIO:.0%}"
        )

    latest_safe_date = common._latest_clock_safe_us_date()
    cutoff = common._latest_cutoff(
        price_data,
        acwi_frame,
        latest_clock_safe_us_date=latest_safe_date,
    )
    if cutoff < pd.Timestamp(core_backtest_periods.OOS_START):
        raise RuntimeError("local cache has no observed OOS period")
    price_data, provenance_rows = common._drop_latest_clock_safe_session_per_ticker(
        price_data,
        latest_safe_date,
    )
    price_data = {
        ticker: frame.loc[:cutoff].copy()
        if frame is not None and not frame.empty
        else frame
        for ticker, frame in price_data.items()
    }
    acwi_frame = acwi_frame.loc[:cutoff].copy()
    windows = {
        "IS": core_backtest_periods.selection_window(),
        "OBSERVED_OOS": {
            "evaluation_start": core_backtest_periods.OOS_START,
            "evaluation_end": cutoff.date().isoformat(),
        },
    }

    features = portfolio_hmm_overlay.build_regime_features(
        _market_close(acwi_frame),
        _universe_close(price_data, universe),
        min_constituents=max(2, len(universe) // 2),
    )
    rule_probability = portfolio_hmm_overlay.rule_trend_probabilities(features)
    hmm_probability = portfolio_hmm_overlay.walk_forward_trend_probabilities(features)
    price_loader = common._loader(price_data)
    market_loader = common._market_loader(acwi_frame)
    portfolio_policy = core_portfolio_allocator.PortfolioPolicy(
        position_mode="LONG_SHORT_NEUTRAL"
    )
    probabilities = {
        "BASELINE": None,
        "RULE": rule_probability,
        "HMM": hmm_probability,
    }
    policies = {
        "BASELINE": None,
        "RULE": portfolio_hmm_overlay.RegimeSizingPolicy(
            probabilities["RULE"],
            max_name_weight=portfolio_policy.max_name_weight,
        ),
        "HMM": portfolio_hmm_overlay.RegimeSizingPolicy(
            probabilities["HMM"],
            max_name_weight=portfolio_policy.max_name_weight,
        ),
    }

    summaries = []
    failures = []
    decisions = []
    for window_name, window in windows.items():
        print(f"{window_name}: collecting three-strategy signals", flush=True)
        trades, window_failures = _combined_trades(
            universe,
            price_data,
            window,
            window_name,
        )
        failures.extend(window_failures)
        for variant, policy in policies.items():
            decision_log = [] if policy is not None else None
            performance, metrics = common._evaluate(
                trades,
                universe,
                window,
                price_loader,
                market_loader,
                portfolio_policy,
                policy=policy,
                decision_log=decision_log,
            )
            summaries.append(
                _summary(
                    window_name,
                    variant,
                    performance,
                    metrics,
                    probabilities[variant],
                    window,
                )
            )
            if decision_log:
                decisions.extend(
                    {"window": window_name, "variant": variant, **row}
                    for row in decision_log
                )

    payload = {
        "schema_version": "portfolio-hmm-experiment-1.0",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "research_only": True,
        "group": group,
        "strategies": list(STRATEGIES),
        "cutoff": cutoff.date().isoformat(),
        "provenance_guard_rows": provenance_rows,
        "feature_columns": list(portfolio_hmm_overlay.FEATURE_COLUMNS),
        "probabilities": [
            {
                "date": date.date().isoformat(),
                "rule_p_trend": (
                    float(rule_probability.get(date))
                    if pd.notna(rule_probability.get(date))
                    else None
                ),
                "hmm_p_trend": (
                    float(hmm_probability.get(date))
                    if pd.notna(hmm_probability.get(date))
                    else None
                ),
            }
            for date in features.index
        ],
        "summaries": summaries,
        "ticker_failures": failures,
        "decisions": decisions,
    }
    if output:
        _write_output(output, payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="CSQ_UNIV")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--confirm-long-run", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run_experiment(
        group=args.group,
        estimate_only=args.estimate_only,
        confirm_long_run=args.confirm_long_run,
        output=args.output,
    )
    print(json.dumps(_jsonable(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
