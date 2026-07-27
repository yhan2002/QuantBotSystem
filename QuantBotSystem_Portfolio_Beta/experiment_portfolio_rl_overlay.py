"""Developer-only portfolio-aware continuous sizing RL experiment.

This script reads only the local cache.  Strategy signals and exits remain
unchanged; a NumPy-only policy can scale each next-open entry and the whole
new-entry cohort by values in [0, 1].  Policies train on fixed IS only.  The
already-observed 2026 window is reported as research validation, not a fresh
final holdout.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, time, timedelta
from functools import wraps
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

import pandas as pd

import analyze_strategy_universe_direction as performance_cube
import config
import core_backtest_periods
import core_market_data_store
import core_portfolio_allocator
import core_portfolio_performance
import core_signal_scoring
import portfolio_rl_overlay
import runtime_settings


STRATEGY_ALIASES = {
    "MOMENTUM": "Momentum",
    "MA": "Momentum",
    "BOX": "Box Range",
    "BOX_RANGE": "Box Range",
    "REVERSION": "Reversion",
}
DEFAULT_GROUPS = ("CSQ_UNIV",)
DEFAULT_STRATEGIES = ("REVERSION",)
SCHEMA_VERSION = "portfolio-rl-experiment-1.1"
EXPERIMENT_POSITION_MODE = "LONG_SHORT_NEUTRAL"
_SAFE_STAMP = re.compile(r"^[A-Za-z0-9_-]+$")
MINIMUM_DATA_COVERAGE_RATIO = 0.95
_ACTIVE_CHECKPOINT_PATH = None


def _write_csv(path, rows):
    rows = list(rows)
    columns = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if columns:
            writer.writeheader()
            writer.writerows(rows)


def _write_json_atomic(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _checkpoint_failures(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        global _ACTIVE_CHECKPOINT_PATH
        _ACTIVE_CHECKPOINT_PATH = None
        try:
            return function(*args, **kwargs)
        except BaseException as exc:
            checkpoint = _ACTIVE_CHECKPOINT_PATH
            if checkpoint is not None:
                try:
                    payload = {}
                    if checkpoint.exists():
                        payload = json.loads(
                            checkpoint.read_text(encoding="utf-8")
                        )
                    previous_status = payload.get("status")
                    payload.update(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": (
                                "INTERRUPTED"
                                if isinstance(exc, KeyboardInterrupt)
                                else "FAILED"
                            ),
                            "previous_status": previous_status,
                            "failure_phase": payload.get(
                                "phase", "UNHANDLED_EXCEPTION"
                            ),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "failed_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        }
                    )
                    _write_json_atomic(checkpoint, payload)
                except (OSError, ValueError, TypeError):
                    pass
            raise
        finally:
            _ACTIVE_CHECKPOINT_PATH = None

    return wrapped


def _prepare_output_paths(output_dir, timestamp):
    directory = (
        Path(output_dir)
        if output_dir
        else Path(runtime_settings.EXPORT_DIR)
    )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if not _SAFE_STAMP.fullmatch(str(stamp)):
        raise ValueError(
            "timestamp may contain only letters, numbers, underscore, "
            "and hyphen"
        )
    probe = directory / f".portfolio_rl_overlay_{stamp}.write_test"
    probe.write_text("write test", encoding="utf-8")
    probe.unlink()
    prefix = directory / f"portfolio_rl_overlay_{stamp}"
    paths = {
        "summary": Path(f"{prefix}_summary.csv"),
        "decisions": Path(f"{prefix}_decisions.csv"),
        "seed_metrics": Path(f"{prefix}_seed_metrics.csv"),
        "training_history": Path(f"{prefix}_training_history.csv"),
        "daily_nav": Path(f"{prefix}_daily_nav.csv"),
        "feature_skips": Path(f"{prefix}_feature_skips.csv"),
        "ticker_failures": Path(f"{prefix}_ticker_failures.csv"),
        "ledger_events": Path(f"{prefix}_ledger_events.csv"),
        "model": Path(f"{prefix}_model.json"),
        "checkpoint": Path(f"{prefix}_checkpoint.json"),
    }
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(
            "experiment output already exists: " + ", ".join(existing)
        )
    return paths


def _strategy_name(value):
    key = str(value).strip().upper().replace(" ", "_")
    if key not in STRATEGY_ALIASES:
        raise ValueError(
            f"Unknown strategy {value!r}; choose MOMENTUM, BOX, or REVERSION"
        )
    return STRATEGY_ALIASES[key]


def _universe_specs(
    groups,
    *,
    combine_groups=False,
    combined_universe_name=None,
):
    specs = {}
    for group in groups:
        if group not in config.universe_groups:
            raise KeyError(f"Unknown universe group: {group}")
        specs[group] = sorted(
            set(config.universe_groups[group])
            - {core_portfolio_performance.MARKET_BENCHMARK_TICKER}
        )
        if not specs[group]:
            raise ValueError(f"Universe group is empty: {group}")
    if not combine_groups:
        return specs
    if len(specs) < 2:
        raise ValueError(
            "combine_groups requires at least two source universe groups"
        )
    combined_name = str(
        combined_universe_name or "COMBINED_GROUPS"
    ).strip()
    if not combined_name:
        raise ValueError("combined_universe_name must not be empty")
    combined = sorted(set().union(*(set(values) for values in specs.values())))
    if not combined:
        raise ValueError("Combined universe is empty")
    return {combined_name: combined}


def _collect_ticker_results(strategy, tickers, price_data, window):
    results = {}
    for completed, ticker in enumerate(tickers, start=1):
        try:
            results[ticker] = performance_cube._runner(
                strategy,
                ticker,
                price_data[ticker],
                None,
                window,
            )
        except Exception as exc:
            results[ticker] = exc
        if completed == 1 or completed % 25 == 0 or completed == len(tickers):
            print(
                f"  signals {completed}/{len(tickers)}",
                flush=True,
            )
    return results


def _scored_trades(strategy, universe, ticker_results):
    result = performance_cube.aggregate_cached_results(
        universe, ticker_results
    )
    raw = []
    for trade in result["trades"]:
        item = dict(trade)
        item["strategy"] = strategy
        raw.append(item)
    if not raw:
        return [], result
    scored = core_signal_scoring.annotate_trades_by_entry_date(
        raw, strategy=strategy
    )
    return scored, result


def _latest_clock_safe_us_date():
    new_york_now = datetime.now(ZoneInfo("America/New_York"))
    return (
        new_york_now.date()
        if new_york_now.time() >= time(18, 0)
        else new_york_now.date() - timedelta(days=1)
    )


def _drop_latest_clock_safe_session_per_ticker(
    price_data,
    latest_clock_safe_us_date,
):
    clock_safe_date = pd.Timestamp(latest_clock_safe_us_date).normalize()
    guarded = {}
    removed = {}
    for ticker, frame in price_data.items():
        if frame is None or frame.empty:
            guarded[ticker] = frame
            continue
        normalized_dates = pd.DatetimeIndex(
            pd.to_datetime(frame.index)
        ).normalize()
        eligible_dates = normalized_dates[
            normalized_dates <= clock_safe_date
        ].unique()
        if len(eligible_dates) == 0:
            guarded[ticker] = frame.copy()
            continue
        unproven_date = eligible_dates.max()
        keep = normalized_dates != unproven_date
        guarded[ticker] = frame.loc[keep].copy()
        removed[ticker] = {
            "date": unproven_date.date().isoformat(),
            "rows": int((~keep).sum()),
        }
    return guarded, removed


def _latest_cutoff(
    price_data,
    acwi_frame,
    latest_clock_safe_us_date=None,
):
    ticker_latest = [
        pd.Timestamp(frame.index.max()).normalize()
        for frame in price_data.values()
        if frame is not None and not frame.empty
    ]
    if not ticker_latest:
        raise RuntimeError("No local market data is available")
    if acwi_frame is None or acwi_frame.empty:
        raise RuntimeError("Local ACWI data is required")
    if latest_clock_safe_us_date is None:
        latest_clock_safe_us_date = _latest_clock_safe_us_date()
    latest_clock_safe_us_date = pd.Timestamp(
        latest_clock_safe_us_date
    ).normalize()
    acwi_dates = (
        pd.DatetimeIndex(pd.to_datetime(acwi_frame.index))
        .normalize()
        .unique()
        .sort_values()
    )
    clock_safe_acwi_dates = acwi_dates[
        acwi_dates <= latest_clock_safe_us_date
    ]
    if len(clock_safe_acwi_dates) < 2:
        raise RuntimeError(
            "At least two clock-safe ACWI sessions are required because "
            "local OHLCV rows have no post-close update provenance"
        )
    provenance_safe_acwi_date = clock_safe_acwi_dates[-2]
    return min(
        pd.Timestamp(core_backtest_periods.OOS_END),
        provenance_safe_acwi_date,
        max(ticker_latest),
    ).normalize()


def _data_snapshot(price_data, acwi_frame, cutoff):
    rows = {}
    future_dated = {}
    for ticker, frame in price_data.items():
        if frame is None or frame.empty:
            rows[ticker] = {"rows": 0, "first_date": None, "last_date": None}
            continue
        normalized = frame.copy()
        normalized.index = pd.to_datetime(normalized.index).normalize()
        rows[ticker] = {
            "rows": int(len(normalized)),
            "first_date": normalized.index.min().date().isoformat(),
            "last_date": normalized.index.max().date().isoformat(),
        }
        count = int((normalized.index > cutoff).sum())
        if count:
            future_dated[ticker] = count
    acwi = {}
    if acwi_frame is not None and not acwi_frame.empty:
        index = pd.to_datetime(acwi_frame.index).normalize()
        acwi = {
            "rows": int(len(acwi_frame)),
            "first_date": index.min().date().isoformat(),
            "last_date": index.max().date().isoformat(),
            "rows_after_cutoff": int((index > cutoff).sum()),
        }
    return {
        "cutoff": cutoff.date().isoformat(),
        "tickers": rows,
        "future_dated_rows_excluded": future_dated,
        "acwi": acwi,
    }


def _git_state():
    root = Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        )
        return {"commit_sha": sha, "dirty_worktree": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit_sha": None, "dirty_worktree": None}


def _loader(price_data):
    def load(ticker):
        return price_data.get(str(ticker).upper(), pd.DataFrame())

    return load


def _market_loader(acwi_frame):
    def load(ticker):
        if ticker != core_portfolio_performance.MARKET_BENCHMARK_TICKER:
            raise KeyError(f"Unsupported market benchmark ticker: {ticker}")
        return acwi_frame

    return load


def _summary_row(
    strategy,
    group,
    window,
    variant,
    performance,
    metrics,
    *,
    seed="",
    observed_validation=False,
    cvar_confidence=0.95,
    coverage=None,
    feasibility=None,
    universe_mode="SEPARATE",
    source_groups=(),
):
    target = core_portfolio_performance.evaluate_return_target(
        metrics["roi_pct"],
        metrics["acwi_roi_pct"],
        None,
    )
    coverage = dict(coverage or {})
    universe_count = int(coverage.get("universe_count") or 0)
    eligible_tickers = int(coverage.get("eligible_tickers") or 0)
    coverage_ratio = (
        eligible_tickers / universe_count if universe_count else 0.0
    )
    data_coverage_status = (
        "OK"
        if coverage_ratio >= MINIMUM_DATA_COVERAGE_RATIO
        else "REVIEW"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy,
        "universe": group,
        "universe_mode": universe_mode,
        "source_groups": "|".join(source_groups),
        "position_mode": EXPERIMENT_POSITION_MODE,
        "window": window,
        "variant": variant,
        "seed": seed,
        "observed_validation_not_final_holdout": observed_validation,
        "evaluation_role": (
            "OBSERVED_VALIDATION_NOT_HOLDOUT"
            if observed_validation
            else "TRAINING_FIT"
        ),
        "deployable": False,
        "period": performance["period"],
        "portfolio_roi_pct": metrics["roi_pct"],
        "portfolio_cagr_pct": metrics["cagr_pct"],
        "portfolio_mdd_pct": metrics["mdd_pct"],
        "portfolio_sharpe": metrics["sharpe"],
        "portfolio_cvar_pct": metrics["cvar"] * 100.0,
        "cvar_confidence": cvar_confidence,
        "acwi_roi_pct": metrics["acwi_roi_pct"],
        "acwi_excess_roi_pct": (
            metrics["roi_pct"] - metrics["acwi_roi_pct"]
            if metrics["roi_pct"] is not None
            and metrics["acwi_roi_pct"] is not None
            else ""
        ),
        "excess_log_return_pct": metrics["excess_log_return"] * 100.0,
        "filled_trades": metrics["filled_trades"],
        "skipped_trades": metrics["skipped_trades"],
        "trade_win_rate_pct": metrics.get("trade_win_rate_pct"),
        "average_trade_return_pct": metrics.get(
            "average_trade_return_pct"
        ),
        "long_filled_trades": metrics.get("long_filled_trades"),
        "long_win_rate_pct": metrics.get("long_win_rate_pct"),
        "long_average_trade_return_pct": metrics.get(
            "long_average_trade_return_pct"
        ),
        "long_average_entry_weight_pct": metrics.get(
            "long_average_entry_weight_pct"
        ),
        "short_filled_trades": metrics.get("short_filled_trades"),
        "short_win_rate_pct": metrics.get("short_win_rate_pct"),
        "short_average_trade_return_pct": metrics.get(
            "short_average_trade_return_pct"
        ),
        "short_average_entry_weight_pct": metrics.get(
            "short_average_entry_weight_pct"
        ),
        "average_gross_pct": metrics["average_gross_pct"],
        "maximum_gross_pct": metrics["maximum_gross_pct"],
        "average_policy_gross_scale": metrics.get(
            "average_policy_gross_scale"
        ),
        "average_policy_trade_multiplier": metrics.get(
            "average_policy_trade_multiplier"
        ),
        "feature_eligible_candidates": metrics.get(
            "feature_eligible_candidates"
        ),
        "feature_eligible_filled_trades": metrics.get(
            "feature_eligible_filled_trades"
        ),
        "passthrough_candidates": metrics.get("passthrough_candidates"),
        "passthrough_filled_trades": metrics.get(
            "passthrough_filled_trades"
        ),
        "eligible_average_effective_scale": metrics.get(
            "eligible_average_effective_scale"
        ),
        "eligible_long_candidates": metrics.get(
            "eligible_long_candidates"
        ),
        "eligible_long_filled_trades": metrics.get(
            "eligible_long_filled_trades"
        ),
        "eligible_long_average_effective_scale": metrics.get(
            "eligible_long_average_effective_scale"
        ),
        "eligible_short_candidates": metrics.get(
            "eligible_short_candidates"
        ),
        "eligible_short_filled_trades": metrics.get(
            "eligible_short_filled_trades"
        ),
        "eligible_short_average_effective_scale": metrics.get(
            "eligible_short_average_effective_scale"
        ),
        "average_net_exposure_pct": metrics.get(
            "average_net_exposure_pct"
        ),
        "universe_count": universe_count,
        "data_eligible_tickers": eligible_tickers,
        "data_coverage_pct": coverage_ratio * 100.0,
        "data_coverage_status": data_coverage_status,
        "validation_data_status": (
            "NO_TRADES"
            if int(metrics["filled_trades"]) == 0
            else "AVAILABLE"
        ),
        "policy_constraint_satisfied": (
            feasibility.get("satisfied")
            if feasibility is not None
            else None
        ),
        "policy_constraint_failed_checks": (
            "|".join(feasibility.get("failed_checks", []))
            if feasibility is not None
            else ""
        ),
        "acwi_target_status": target["status"],
        "strictly_beats_positive_acwi": target["passes_benchmark_roi"],
        "execution_equivalent": True,
        "trading_costs": 0,
    }


def _decision_rows(
    strategy,
    group,
    window,
    performance,
    decision_log,
):
    logged = {row["trade_id"]: row for row in decision_log}
    rows = []
    for trade in performance["ledger"]["trades"]:
        trade_id = str(trade.get("trade_id") or "")
        decision = logged.get(trade_id, {})
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "strategy": strategy,
                "universe": group,
                "window": window,
                "trade_id": trade_id,
                "ticker": trade.get("ticker"),
                "side": trade.get("side"),
                "signal_date": trade.get("signal_date"),
                "entry_date": trade.get("entry_date"),
                "exit_date": trade.get("exit_date"),
                "decision_asof_date": decision.get(
                    "decision_asof_date", ""
                ),
                "gross_scale": decision.get("gross_scale", ""),
                "trade_multiplier": decision.get(
                    "trade_multiplier", ""
                ),
                "base_requested_weight": trade.get(
                    "base_requested_weight", ""
                ),
                "requested_weight": trade.get("requested_weight", ""),
                "entry_weight_pct": trade.get("entry_weight_pct", ""),
                "allocation_status": trade.get("allocation_status", ""),
                "return_pct": trade.get("return", ""),
                "realized_pnl": trade.get("realized_pnl", ""),
                "feature_asof_date": trade.get("feature_asof_date", ""),
                "policy_feature_schema": trade.get(
                    "policy_feature_schema", ""
                ),
                "policy_feature_eligible": trade.get(
                    "policy_feature_eligible", ""
                ),
                "policy_feature_skip_reason": trade.get(
                    "policy_feature_skip_reason", ""
                ),
                "policy_observation_id": trade.get(
                    "policy_observation_id", ""
                ),
                "policy_features_json": json.dumps(
                    trade.get("policy_features", [])
                ),
                "allocated_base_notional": trade.get(
                    "allocated_base_notional", ""
                ),
                "allocated_gross_notional": trade.get(
                    "allocated_gross_notional", ""
                ),
                "quantity": trade.get("quantity", ""),
                "exit_events_json": json.dumps(
                    trade.get("exit_events", []),
                    default=str,
                ),
                "execution_equivalent": True,
            }
        )
    return rows


def _ledger_event_rows(
    strategy,
    group,
    window,
    variant,
    performance,
):
    rows = []
    for event in performance["ledger"].get("events", []):
        row = {
            "schema_version": SCHEMA_VERSION,
            "strategy": strategy,
            "universe": group,
            "window": window,
            "variant": variant,
        }
        for key, value in event.items():
            if isinstance(value, (dict, list, tuple)):
                row[key] = json.dumps(value, default=str)
            elif isinstance(value, pd.Timestamp):
                row[key] = value.isoformat()
            else:
                row[key] = value
        rows.append(row)
    return rows


def _daily_rows(strategy, group, window, variant, performance):
    rows = []
    for date, values in performance["daily"].iterrows():
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "strategy": strategy,
                "universe": group,
                "window": window,
                "variant": variant,
                "date": date.date().isoformat(),
                "strategy_return_pct": (
                    float(values["strategy_return"]) * 100.0
                ),
                "strategy_nav": float(values["strategy_nav"]),
                "acwi_return_pct": (
                    float(values["market_benchmark_return"]) * 100.0
                ),
                "acwi_nav": float(values["market_benchmark_nav"]),
                "gross_exposure_pct": float(
                    values["gross_exposure_pct"]
                ),
                "net_exposure_pct": float(values["net_exposure_pct"]),
                "active_positions": int(values["active_positions"]),
            }
        )
    return rows


def _evaluate(
    trades,
    universe,
    window,
    price_loader,
    market_loader,
    portfolio_policy,
    policy=None,
    decision_log=None,
    cvar_confidence=0.95,
):
    performance = portfolio_rl_overlay.evaluate_policy(
        trades,
        universe,
        window["evaluation_start"],
        window["evaluation_end"],
        price_loader=price_loader,
        market_benchmark_loader=market_loader,
        portfolio_policy=portfolio_policy,
        sizing_policy=policy,
        decision_log=decision_log,
    )
    metrics = portfolio_rl_overlay.policy_metrics(
        performance,
        confidence=cvar_confidence,
    )
    return performance, metrics


@_checkpoint_failures
def run_experiment(
    *,
    groups=DEFAULT_GROUPS,
    combine_groups=False,
    combined_universe_name=None,
    strategies=DEFAULT_STRATEGIES,
    seeds=portfolio_rl_overlay.DEFAULT_SEEDS,
    training_config=None,
    output_dir=None,
    timestamp=None,
    confirm_long_run=False,
    estimate_only=False,
):
    groups = tuple(groups)
    strategy_names = tuple(_strategy_name(item) for item in strategies)
    seeds = tuple(int(seed) for seed in seeds)
    if not groups:
        raise ValueError("at least one universe group is required")
    if len(set(groups)) != len(groups):
        raise ValueError("universe groups must be unique")
    if combine_groups and len(groups) < 2:
        raise ValueError(
            "combine_groups requires at least two source universe groups"
        )
    if combined_universe_name is not None and not combine_groups:
        raise ValueError(
            "combined_universe_name requires combine_groups=True"
        )
    if not strategy_names:
        raise ValueError("at least one strategy is required")
    if len(set(strategy_names)) != len(strategy_names):
        raise ValueError(
            "strategies must be unique after alias normalization"
        )
    if not seeds:
        raise ValueError("at least one seed is required")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    if any(seed < 0 for seed in seeds):
        raise ValueError("seeds must be non-negative")
    training_config = (
        training_config
        or portfolio_rl_overlay.EvolutionStrategyConfig()
    )
    universes = _universe_specs(
        groups,
        combine_groups=combine_groups,
        combined_universe_name=combined_universe_name,
    )
    combination_count = len(strategy_names) * len(universes)
    portfolio_policy = core_portfolio_allocator.PortfolioPolicy(
        position_mode=EXPERIMENT_POSITION_MODE
    )
    source_group_counts = {
        group: len(
            set(config.universe_groups[group])
            - {core_portfolio_performance.MARKET_BENCHMARK_TICKER}
        )
        for group in groups
    }
    universe_construction = {
        "mode": "COMBINED_UNION" if combine_groups else "SEPARATE",
        "source_groups": source_group_counts,
        "deduplicated_members_removed": (
            sum(source_group_counts.values())
            - sum(len(values) for values in universes.values())
            if combine_groups
            else 0
        ),
        "portfolio_universes": {
            name: {
                "count": len(values),
                "members": list(values),
            }
            for name, values in universes.items()
        },
    }
    checkpoint_scope = {
        "groups": list(groups),
        "universe_construction": universe_construction,
        "position_mode": portfolio_policy.position_mode,
        "strategies": list(strategy_names),
        "seeds": list(seeds),
        "training_config": asdict(training_config),
    }
    replays_per_seed = 3 + training_config.iterations * (
        2 * training_config.population + 1
    )
    estimated_replays = combination_count * (
        len(seeds) * replays_per_seed + 4
    )
    print(
        "Resolved portfolio universes: "
        + ", ".join(
            f"{name}={len(values)}"
            for name, values in universes.items()
        ),
        flush=True,
    )
    print(
        "Estimated upper-bound full portfolio ledger replays: "
        f"{estimated_replays:,} "
        f"({combination_count} strategy/universe combinations)",
        flush=True,
    )
    if estimate_only:
        return {
            "estimated_upper_bound_ledger_replays": estimated_replays,
            "strategy_universe_combinations": combination_count,
            "portfolio_universes": {
                name: len(values) for name, values in universes.items()
            },
            "universe_construction": universe_construction,
            "position_mode": portfolio_policy.position_mode,
        }
    if not confirm_long_run:
        raise RuntimeError(
            "long portfolio experiment not confirmed; inspect the replay "
            "estimate above, then rerun with --confirm-long-run"
        )
    paths = _prepare_output_paths(output_dir, timestamp)
    global _ACTIVE_CHECKPOINT_PATH
    _ACTIVE_CHECKPOINT_PATH = paths["checkpoint"]
    _write_json_atomic(
        paths["checkpoint"],
        {
            "schema_version": SCHEMA_VERSION,
            "status": "STARTED",
            "estimated_upper_bound_ledger_replays": estimated_replays,
            **checkpoint_scope,
            "completed_seeds": [],
        },
    )
    master_universe = sorted(
        set().union(*(set(values) for values in universes.values()))
    )
    print(
        f"Loading local OHLCV once: {len(master_universe)} unique tickers",
        flush=True,
    )
    price_data = {
        ticker: core_market_data_store.get_price_data(
            ticker, period_days=1060
        )
        for ticker in master_universe
    }
    acwi_ticker = core_portfolio_performance.MARKET_BENCHMARK_TICKER
    acwi_frame = core_market_data_store.get_price_data(
        acwi_ticker, period_days=1060
    )
    if acwi_frame is None or acwi_frame.empty:
        raise RuntimeError("Local ACWI data is required")
    latest_clock_safe_us_date = _latest_clock_safe_us_date()
    cutoff = _latest_cutoff(
        price_data,
        acwi_frame,
        latest_clock_safe_us_date=latest_clock_safe_us_date,
    )
    if cutoff < pd.Timestamp(core_backtest_periods.OOS_START):
        raise RuntimeError(
            "local cache has no observed OOS period through the safe cutoff"
        )
    price_data, provenance_guard_rows = (
        _drop_latest_clock_safe_session_per_ticker(
            price_data,
            latest_clock_safe_us_date,
        )
    )
    data_snapshot = _data_snapshot(price_data, acwi_frame, cutoff)
    data_snapshot["provenance_guard"] = {
        "reason": "local OHLCV rows have no post-close update timestamp",
        "latest_clock_safe_us_date": (
            pd.Timestamp(latest_clock_safe_us_date).date().isoformat()
        ),
        "acwi_clock_safe_sessions_excluded": 1,
        "per_ticker_latest_clock_safe_sessions_removed": (
            provenance_guard_rows
        ),
    }
    price_data = {
        ticker: (
            frame.loc[:cutoff].copy()
            if frame is not None and not frame.empty
            else frame
        )
        for ticker, frame in price_data.items()
    }
    acwi_frame = acwi_frame.loc[:cutoff].copy()
    price_loader = _loader(price_data)
    market_loader = _market_loader(acwi_frame)
    windows = {
        "IS": core_backtest_periods.selection_window(),
        "OBSERVED_OOS": {
            "evaluation_start": core_backtest_periods.OOS_START,
            "evaluation_end": cutoff.date().isoformat(),
        },
    }

    summary_rows = []
    seed_rows = []
    decision_rows = []
    daily_rows = []
    history_rows = []
    model_payloads = []
    feature_skip_rows = []
    ticker_failure_rows = []
    ledger_event_rows = []
    completed_seed_models = []

    for strategy in strategy_names:
        print(f"\n{strategy}: collecting fixed IS signals", flush=True)
        is_results = _collect_ticker_results(
            strategy,
            master_universe,
            price_data,
            windows["IS"],
        )
        print(f"{strategy}: collecting observed OOS signals", flush=True)
        oos_results = _collect_ticker_results(
            strategy,
            master_universe,
            price_data,
            windows["OBSERVED_OOS"],
        )
        for window_name, results in (
            ("IS", is_results),
            ("OBSERVED_OOS", oos_results),
        ):
            ticker_failure_rows.extend(
                {
                    "schema_version": SCHEMA_VERSION,
                    "strategy": strategy,
                    "window": window_name,
                    "ticker": ticker,
                    "error_type": type(result).__name__,
                    "error": str(result),
                }
                for ticker, result in results.items()
                if isinstance(result, Exception)
            )
        for group, universe in universes.items():
            print(f"\n{strategy} | {group}", flush=True)
            is_trades, is_coverage = _scored_trades(
                strategy, universe, is_results
            )
            oos_trades, oos_coverage = _scored_trades(
                strategy, universe, oos_results
            )
            is_coverage_ratio = (
                float(is_coverage["eligible_tickers"])
                / float(is_coverage["universe_count"])
                if is_coverage["universe_count"]
                else 0.0
            )
            if is_coverage_ratio < MINIMUM_DATA_COVERAGE_RATIO:
                print(
                    "  IS data coverage below "
                    f"{MINIMUM_DATA_COVERAGE_RATIO:.0%}; skipped",
                    flush=True,
                )
                summary_rows.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "strategy": strategy,
                        "universe": group,
                        "universe_mode": universe_construction["mode"],
                        "source_groups": "|".join(groups),
                        "position_mode": portfolio_policy.position_mode,
                        "window": "IS",
                        "variant": "NO_MODEL",
                        "run_status": "DATA_COVERAGE_REVIEW",
                        "evaluation_role": "TRAINING_FIT",
                        "deployable": False,
                        "universe_count": is_coverage["universe_count"],
                        "data_eligible_tickers": is_coverage[
                            "eligible_tickers"
                        ],
                        "data_coverage_pct": is_coverage_ratio * 100.0,
                    }
                )
                continue
            if not is_trades:
                print("  no IS trades; skipped", flush=True)
                summary_rows.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "strategy": strategy,
                        "universe": group,
                        "universe_mode": universe_construction["mode"],
                        "source_groups": "|".join(groups),
                        "position_mode": portfolio_policy.position_mode,
                        "window": "IS",
                        "variant": "NO_MODEL",
                        "run_status": "NO_IS_TRADES",
                        "evaluation_role": "TRAINING_FIT",
                        "deployable": False,
                        "universe_count": is_coverage["universe_count"],
                        "data_eligible_tickers": is_coverage[
                            "eligible_tickers"
                        ],
                    }
                )
                continue
            is_trades = [
                {**trade, "trade_id": f"IS{index:06d}"}
                for index, trade in enumerate(is_trades, start=1)
            ]
            oos_trades = [
                {**trade, "trade_id": f"OOS{index:06d}"}
                for index, trade in enumerate(oos_trades, start=1)
            ]

            prepared_is = (
                portfolio_rl_overlay.prepare_point_in_time_trades(
                    is_trades,
                    price_data,
                    acwi_frame,
                )
            )
            prepared_oos = (
                portfolio_rl_overlay.prepare_point_in_time_trades(
                    oos_trades,
                    price_data,
                    acwi_frame,
                    scaler=prepared_is.scaler,
                )
                if oos_trades
                else None
            )
            is_policy_trades = list(prepared_is.trades)
            oos_policy_trades = (
                list(prepared_oos.trades)
                if prepared_oos is not None
                else []
            )
            for window_name, skipped in (
                ("IS", prepared_is.skipped),
                (
                    "OBSERVED_OOS",
                    prepared_oos.skipped if prepared_oos else (),
                ),
            ):
                feature_skip_rows.extend(
                    {
                        "strategy": strategy,
                        "universe": group,
                        "window": window_name,
                        **row,
                    }
                    for row in skipped
                )

            baseline_is, baseline_is_metrics = _evaluate(
                is_policy_trades,
                universe,
                windows["IS"],
                price_loader,
                market_loader,
                portfolio_policy,
                cvar_confidence=training_config.cvar_confidence,
            )
            summary_rows.append(
                _summary_row(
                    strategy,
                    group,
                    "IS",
                    "BASELINE",
                    baseline_is,
                    baseline_is_metrics,
                    cvar_confidence=training_config.cvar_confidence,
                    coverage=is_coverage,
                    universe_mode=universe_construction["mode"],
                    source_groups=groups,
                )
            )
            daily_rows.extend(
                _daily_rows(
                    strategy,
                    group,
                    "IS",
                    "BASELINE",
                    baseline_is,
                )
            )
            ledger_event_rows.extend(
                _ledger_event_rows(
                    strategy,
                    group,
                    "IS",
                    "BASELINE",
                    baseline_is,
                )
            )

            trained = []
            for seed in seeds:
                print(f"  training seed {seed}", flush=True)

                def progress(iteration, total, row):
                    print(
                        f"    {iteration:>2}/{total} | "
                        f"ROI {row['roi_pct']:+.2f}% | "
                        f"CVaR {row['cvar'] * 100.0:.2f}% | "
                        f"objective {row['objective']:+.4f}",
                        flush=True,
                    )

                try:
                    result = portfolio_rl_overlay.train_policy(
                        is_policy_trades,
                        universe,
                        windows["IS"]["evaluation_start"],
                        windows["IS"]["evaluation_end"],
                        price_loader=price_loader,
                        market_benchmark_loader=market_loader,
                        portfolio_policy=portfolio_policy,
                        config=training_config,
                        seed=seed,
                        progress_callback=progress,
                    )
                except Exception as exc:
                    _write_json_atomic(
                        paths["checkpoint"],
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "FAILED",
                            "phase": "TRAINING",
                            **checkpoint_scope,
                            "strategy": strategy,
                            "universe": group,
                            "seed": seed,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "completed_seed_models": (
                                completed_seed_models
                            ),
                        },
                    )
                    raise
                trained.append(result)
                completed_seed_models.append(
                    {
                        "strategy": strategy,
                        "universe": group,
                        "seed": seed,
                        "feature_scaler": prepared_is.scaler.to_dict(),
                        "policy": result.policy.to_dict(),
                        "trained_metrics": dict(result.trained_metrics),
                        "selected_iteration": result.selected_iteration,
                        "constraint_satisfied": (
                            result.constraint_satisfied
                        ),
                        "feasibility": dict(result.feasibility),
                    }
                )
                history_rows.extend(
                    {
                        "strategy": strategy,
                        "universe": group,
                        **row,
                    }
                    for row in result.history
                )
                seed_rows.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "strategy": strategy,
                        "universe": group,
                        "seed": seed,
                        "is_roi_pct": result.trained_metrics["roi_pct"],
                        "is_acwi_roi_pct": result.trained_metrics[
                            "acwi_roi_pct"
                        ],
                        "is_excess_log_return_pct": (
                            result.trained_metrics["excess_log_return"]
                            * 100.0
                        ),
                        "is_cvar_pct": (
                            result.trained_metrics["cvar"] * 100.0
                        ),
                        "cvar_confidence": (
                            training_config.cvar_confidence
                        ),
                        "is_mdd_pct": result.trained_metrics["mdd_pct"],
                        "is_trade_win_rate_pct": (
                            result.trained_metrics.get(
                                "trade_win_rate_pct"
                            )
                        ),
                        "is_average_gross_pct": (
                            result.trained_metrics[
                                "average_gross_pct"
                            ]
                        ),
                        "is_average_policy_trade_multiplier": (
                            result.trained_metrics.get(
                                "average_policy_trade_multiplier"
                            )
                        ),
                        "is_feature_eligible_candidates": (
                            result.trained_metrics[
                                "feature_eligible_candidates"
                            ]
                        ),
                        "is_feature_eligible_filled_trades": (
                            result.trained_metrics[
                                "feature_eligible_filled_trades"
                            ]
                        ),
                        "is_passthrough_candidates": (
                            result.trained_metrics[
                                "passthrough_candidates"
                            ]
                        ),
                        "is_eligible_average_effective_scale": (
                            result.trained_metrics[
                                "eligible_average_effective_scale"
                            ]
                        ),
                        "is_average_net_exposure_pct": (
                            result.trained_metrics[
                                "average_net_exposure_pct"
                            ]
                        ),
                        "cvar_limit_pct": result.cvar_limit * 100.0,
                        "final_dual_multiplier": (
                            result.final_dual_multiplier
                        ),
                        "selected_iteration": (
                            result.selected_iteration
                        ),
                        "constraint_satisfied": (
                            result.constraint_satisfied
                        ),
                        "minimum_average_gross_pct": (
                            result.minimum_average_gross_pct
                        ),
                        "minimum_eligible_effective_scale": (
                            result.minimum_eligible_effective_scale
                        ),
                        "failed_constraint_checks": "|".join(
                            result.feasibility["failed_checks"]
                        ),
                    }
                )
                _write_csv(paths["seed_metrics"], seed_rows)
                _write_csv(paths["training_history"], history_rows)
                _write_json_atomic(
                    paths["checkpoint"],
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "RUNNING",
                        "estimated_upper_bound_ledger_replays": (
                            estimated_replays
                        ),
                        **checkpoint_scope,
                        "completed_seed_models": completed_seed_models,
                    },
                )

            ensemble = portfolio_rl_overlay.EnsembleSizingPolicy(
                tuple(item.policy for item in trained)
            )
            ensemble_is_log = []
            ensemble_is, ensemble_is_metrics = _evaluate(
                is_policy_trades,
                universe,
                windows["IS"],
                price_loader,
                market_loader,
                portfolio_policy,
                policy=ensemble,
                decision_log=ensemble_is_log,
                cvar_confidence=training_config.cvar_confidence,
            )
            ensemble_feasibility = (
                portfolio_rl_overlay.policy_feasibility(
                    ensemble_is_metrics,
                    baseline_is_metrics,
                    training_config,
                )
            )
            if not ensemble_feasibility["satisfied"]:
                _write_json_atomic(
                    paths["checkpoint"],
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "FAILED",
                        "phase": "ENSEMBLE_IS_FEASIBILITY",
                        **checkpoint_scope,
                        "strategy": strategy,
                        "universe": group,
                        "failed_checks": ensemble_feasibility[
                            "failed_checks"
                        ],
                        "completed_seed_models": completed_seed_models,
                    },
                )
                raise RuntimeError(
                    "RL ensemble failed IS feasibility gates: "
                    + ", ".join(
                        ensemble_feasibility["failed_checks"]
                    )
                )
            summary_rows.append(
                _summary_row(
                    strategy,
                    group,
                    "IS",
                    "RL_ENSEMBLE",
                    ensemble_is,
                    ensemble_is_metrics,
                    cvar_confidence=training_config.cvar_confidence,
                    coverage=is_coverage,
                    feasibility=ensemble_feasibility,
                    universe_mode=universe_construction["mode"],
                    source_groups=groups,
                )
            )
            daily_rows.extend(
                _daily_rows(
                    strategy,
                    group,
                    "IS",
                    "RL_ENSEMBLE",
                    ensemble_is,
                )
            )
            ledger_event_rows.extend(
                _ledger_event_rows(
                    strategy,
                    group,
                    "IS",
                    "RL_ENSEMBLE",
                    ensemble_is,
                )
            )
            decision_rows.extend(
                _decision_rows(
                    strategy,
                    group,
                    "IS",
                    ensemble_is,
                    ensemble_is_log,
                )
            )

            baseline_oos, baseline_oos_metrics = _evaluate(
                oos_policy_trades,
                universe,
                windows["OBSERVED_OOS"],
                price_loader,
                market_loader,
                portfolio_policy,
                cvar_confidence=training_config.cvar_confidence,
            )
            ensemble_oos_log = []
            ensemble_oos, ensemble_oos_metrics = _evaluate(
                oos_policy_trades,
                universe,
                windows["OBSERVED_OOS"],
                price_loader,
                market_loader,
                portfolio_policy,
                policy=ensemble,
                decision_log=ensemble_oos_log,
                cvar_confidence=training_config.cvar_confidence,
            )
            for variant, performance, metrics in (
                ("BASELINE", baseline_oos, baseline_oos_metrics),
                ("RL_ENSEMBLE", ensemble_oos, ensemble_oos_metrics),
            ):
                summary_rows.append(
                    _summary_row(
                        strategy,
                        group,
                        "OBSERVED_OOS",
                        variant,
                        performance,
                        metrics,
                        observed_validation=True,
                        cvar_confidence=(
                            training_config.cvar_confidence
                        ),
                        coverage=oos_coverage,
                        universe_mode=universe_construction["mode"],
                        source_groups=groups,
                    )
                )
                daily_rows.extend(
                    _daily_rows(
                        strategy,
                        group,
                        "OBSERVED_OOS",
                        variant,
                        performance,
                    )
                )
                ledger_event_rows.extend(
                    _ledger_event_rows(
                        strategy,
                        group,
                        "OBSERVED_OOS",
                        variant,
                        performance,
                    )
                )
            decision_rows.extend(
                _decision_rows(
                    strategy,
                    group,
                    "OBSERVED_OOS",
                    ensemble_oos,
                    ensemble_oos_log,
                )
            )

            model_payloads.append(
                {
                    "strategy": strategy,
                    "universe": group,
                    "position_mode": portfolio_policy.position_mode,
                    "comparison": (
                        "CURRENT_BOX_BASELINE_VS_RL_WEIGHTED_BOX"
                        if strategy == "Box Range"
                        else "CURRENT_STRATEGY_BASELINE_VS_RL_WEIGHTED"
                    ),
                    "deployable": False,
                    "evaluation_role": {
                        "is": "TRAINING_FIT",
                        "oos": "OBSERVED_VALIDATION_NOT_HOLDOUT",
                    },
                    "feature_scaler": prepared_is.scaler.to_dict(),
                    "ensemble": {
                        "aggregation": (
                            "arithmetic mean of per-seed gross and trade "
                            "multipliers"
                        ),
                        "constraint_satisfied": ensemble_feasibility[
                            "satisfied"
                        ],
                        "feasibility": ensemble_feasibility,
                        "is_metrics": ensemble_is_metrics,
                        "observed_oos_metrics": ensemble_oos_metrics,
                    },
                    "policies": [
                        {
                            "seed": item.seed,
                            "policy": item.policy.to_dict(),
                            "cvar_limit": item.cvar_limit,
                            "final_dual_multiplier": (
                                item.final_dual_multiplier
                            ),
                            "selected_iteration": (
                                item.selected_iteration
                            ),
                            "constraint_satisfied": (
                                item.constraint_satisfied
                            ),
                            "minimum_average_gross_pct": (
                                item.minimum_average_gross_pct
                            ),
                            "minimum_eligible_effective_scale": (
                                item.minimum_eligible_effective_scale
                            ),
                            "feasibility": item.feasibility,
                            "trained_metrics": item.trained_metrics,
                        }
                        for item in trained
                    ],
                    "coverage": {
                        "is_universe_count": is_coverage["universe_count"],
                        "is_eligible_tickers": is_coverage[
                            "eligible_tickers"
                        ],
                        "is_feature_trades": sum(
                            bool(
                                trade.get(
                                    "policy_feature_eligible", False
                                )
                            )
                            for trade in is_policy_trades
                        ),
                        "is_feature_skipped": len(prepared_is.skipped),
                        "oos_universe_count": oos_coverage[
                            "universe_count"
                        ],
                        "oos_eligible_tickers": oos_coverage[
                            "eligible_tickers"
                        ],
                        "oos_feature_trades": sum(
                            bool(
                                trade.get(
                                    "policy_feature_eligible", False
                                )
                            )
                            for trade in oos_policy_trades
                        ),
                        "oos_feature_skipped": (
                            len(prepared_oos.skipped)
                            if prepared_oos
                            else 0
                        ),
                    },
                }
            )

    _write_csv(paths["summary"], summary_rows)
    _write_csv(paths["decisions"], decision_rows)
    _write_csv(paths["seed_metrics"], seed_rows)
    _write_csv(paths["training_history"], history_rows)
    _write_csv(paths["daily_nav"], daily_rows)
    _write_csv(paths["feature_skips"], feature_skip_rows)
    _write_csv(paths["ticker_failures"], ticker_failure_rows)
    _write_csv(paths["ledger_events"], ledger_event_rows)
    run_status = "COMPLETED" if model_payloads else "NO_MODELS"
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_schema_version": (
            portfolio_rl_overlay.FEATURE_SCHEMA_VERSION
        ),
        "groups": list(groups),
        "universe_construction": universe_construction,
        "position_mode": portfolio_policy.position_mode,
        "portfolio_policy": {
            "initial_nav": portfolio_policy.initial_nav,
            "max_gross": portfolio_policy.max_gross,
            "max_name_weight": portfolio_policy.max_name_weight,
            "long_cap": portfolio_policy.side_caps[0],
            "short_cap": portfolio_policy.side_caps[1],
        },
        "comparison_design": (
            "same current strategy signals, scores, entries, and exits; "
            "BASELINE has no sizing overlay; RL_ENSEMBLE changes only "
            "entry weights and cohort gross scale"
        ),
        "strategies": list(strategy_names),
        "seeds": list(seeds),
        "training_config": asdict(training_config),
        "run_status": run_status,
        "deployable": False,
        "evaluation_roles": {
            "IS": "TRAINING_FIT",
            "OBSERVED_OOS": "OBSERVED_VALIDATION_NOT_HOLDOUT",
        },
        "git": _git_state(),
        "data_snapshot": data_snapshot,
        "is_window": windows["IS"],
        "observed_oos_window": windows["OBSERVED_OOS"],
        "oos_status": (
            "already observed research validation; not a fresh final holdout"
        ),
        "primary_objective": (
            "maximize portfolio log return minus ACWI log return"
        ),
        "risk_constraint": (
            "portfolio CVaR at configured confidence no worse than "
            "baseline times cvar_tolerance, with eligible-signal and gross "
            "collapse gates"
        ),
        "cvar_confidence": training_config.cvar_confidence,
        "estimated_upper_bound_ledger_replays": estimated_replays,
        "ticker_failure_count": len(ticker_failure_rows),
        "point_in_time_limitations": (
            "Historical tests reuse the current local universe and current "
            "classifications, so survivorship and classification bias remain."
        ),
        "execution": (
            "close-known policy; next-open fill; strategy exits unchanged; "
            "fixed quantities between explicit exits"
        ),
        "costs": 0,
        "model_payloads": model_payloads,
    }
    _write_json_atomic(paths["model"], metadata)
    _write_json_atomic(
        paths["checkpoint"],
        {
            "schema_version": SCHEMA_VERSION,
            "status": run_status,
            "estimated_upper_bound_ledger_replays": estimated_replays,
            "completed_seed_count": len(seed_rows),
            **checkpoint_scope,
            "model_path": str(paths["model"]),
            "completed_seed_models": completed_seed_models,
        },
    )

    print("\nSummary", flush=True)
    for row in summary_rows:
        if row.get("portfolio_roi_pct") is None:
            print(
                f"{row['strategy']:<10} | {row['universe']:<36} | "
                f"{row['window']:<12} | {row['variant']:<11} | "
                f"{row.get('run_status', 'NO_RESULT')}",
                flush=True,
            )
            continue
        print(
            f"{row['strategy']:<10} | {row['universe']:<36} | "
            f"{row['window']:<12} | {row['variant']:<11} | "
            f"ROI {float(row['portfolio_roi_pct']):+7.2f}% | "
            f"ACWI {float(row['acwi_roi_pct']):+7.2f}% | "
            f"CVaR {float(row['portfolio_cvar_pct']):5.2f}% | "
            f"{row['acwi_target_status']}",
            flush=True,
        )
    print("\nExports", flush=True)
    for label, path in paths.items():
        print(f"- {label}: {path}", flush=True)
    if not model_payloads:
        raise RuntimeError(
            "experiment completed without any trainable strategy/universe "
            "model; inspect the summary and checkpoint artifacts"
        )
    return {
        "summary_rows": summary_rows,
        "decision_rows": decision_rows,
        "seed_rows": seed_rows,
        "history_rows": history_rows,
        "daily_rows": daily_rows,
        "paths": paths,
        "metadata": metadata,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--groups",
        nargs="+",
        default=list(DEFAULT_GROUPS),
    )
    parser.add_argument(
        "--combine-groups",
        action="store_true",
        help=(
            "deduplicate all --groups into one portfolio universe instead "
            "of evaluating each group separately"
        ),
    )
    parser.add_argument(
        "--combined-universe-name",
        help=(
            "stable output label for --combine-groups "
            "(default: COMBINED_GROUPS)"
        ),
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=list(DEFAULT_STRATEGIES),
        help="MOMENTUM, BOX, and/or REVERSION",
    )
    parser.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=list(portfolio_rl_overlay.DEFAULT_SEEDS),
    )
    parser.add_argument("--iterations", type=int, default=16)
    parser.add_argument("--population", type=int, default=4)
    parser.add_argument("--noise-std", type=float, default=0.12)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--l2-penalty", type=float, default=1e-4)
    parser.add_argument("--cvar-confidence", type=float, default=0.95)
    parser.add_argument("--cvar-tolerance", type=float, default=1.0)
    parser.add_argument("--dual-learning-rate", type=float, default=2.0)
    parser.add_argument("--minimum-filled-trades", type=int, default=60)
    parser.add_argument(
        "--minimum-feature-eligible-filled-trades",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--minimum-feature-eligible-fill-ratio",
        type=float,
        default=0.50,
    )
    parser.add_argument(
        "--minimum-side-feature-eligible-candidates",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--minimum-average-gross-ratio",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--minimum-eligible-effective-scale",
        type=float,
        default=0.25,
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--timestamp")
    parser.add_argument(
        "--confirm-long-run",
        action="store_true",
        help="required acknowledgement before the local ledger replays start",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="print the upper-bound ledger replay count and exit",
    )
    args = parser.parse_args()
    training_config = portfolio_rl_overlay.EvolutionStrategyConfig(
        iterations=args.iterations,
        population=args.population,
        noise_std=args.noise_std,
        learning_rate=args.learning_rate,
        l2_penalty=args.l2_penalty,
        cvar_confidence=args.cvar_confidence,
        cvar_tolerance=args.cvar_tolerance,
        dual_learning_rate=args.dual_learning_rate,
        minimum_filled_trades=args.minimum_filled_trades,
        minimum_feature_eligible_filled_trades=(
            args.minimum_feature_eligible_filled_trades
        ),
        minimum_feature_eligible_fill_ratio=(
            args.minimum_feature_eligible_fill_ratio
        ),
        minimum_side_feature_eligible_candidates=(
            args.minimum_side_feature_eligible_candidates
        ),
        minimum_average_gross_ratio=(
            args.minimum_average_gross_ratio
        ),
        minimum_eligible_effective_scale=(
            args.minimum_eligible_effective_scale
        ),
    )
    run_experiment(
        groups=args.groups,
        combine_groups=args.combine_groups,
        combined_universe_name=args.combined_universe_name,
        strategies=args.strategies,
        seeds=args.seeds,
        training_config=training_config,
        output_dir=args.output_dir,
        timestamp=args.timestamp,
        confirm_long_run=args.confirm_long_run,
        estimate_only=args.estimate_only,
    )


if __name__ == "__main__":
    main()
