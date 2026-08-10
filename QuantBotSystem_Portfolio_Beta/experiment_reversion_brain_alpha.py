"""Developer-only local-cache comparison for BRAIN Reversion filters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import config
import core_backtest_periods
import core_market_data_store
import core_portfolio_performance
import core_universe_backtest
import strategy_price_liquidity_reversion as reversion


VARIANTS = {
    "BASELINE": reversion.BRAIN_ALPHA_OFF,
    "APAC_EXACT_FILTER": reversion.BRAIN_ALPHA_APAC_EXACT,
    "APAC_UNIT_SAFE_FILTER": reversion.BRAIN_ALPHA_APAC_UNIT_SAFE,
}
HISTORY_DAYS = reversion.BRAIN_ALPHA_HISTORY_DAYS


def _summary(window_name, variant, result, metadata):
    performance = result.get("portfolio_performance") or {}
    strategy = performance.get("strategy") or {}
    market = performance.get("market_benchmark") or {}
    ledger = performance.get("ledger") or {}
    daily = performance.get("daily")
    returns = (
        pd.to_numeric(daily["strategy_return"], errors="coerce").dropna()
        if daily is not None and "strategy_return" in daily
        else pd.Series(dtype=float)
    )
    cutoff = returns.quantile(0.05) if not returns.empty else None
    cvar = (
        float(returns[returns <= cutoff].mean())
        if cutoff is not None and not returns[returns <= cutoff].empty
        else None
    )
    events = ledger.get("events") or []
    turnover = 0.0
    for event in events:
        if event.get("event") == "ENTRY":
            turnover += abs(float(event.get("notional", 0.0)))
        elif event.get("event") == "EXIT":
            turnover += abs(
                float(event.get("quantity", 0.0))
                * float(event.get("price", 0.0))
            )
    average_nav = (
        float(pd.to_numeric(daily["portfolio_value"], errors="coerce").mean())
        if daily is not None and not daily.empty
        else 0.0
    )
    trades = result.get("trades") or []
    country_stats = {}
    holding_days = []
    for trade in trades:
        country = (metadata.get(trade.get("ticker")) or {}).get(
            "listing_country", "UNKNOWN"
        )
        stats = country_stats.setdefault(
            country,
            {"trade_count": 0, "wins": 0, "nav_contribution_pct": 0.0},
        )
        stats["trade_count"] += 1
        stats["wins"] += float(trade.get("return", 0.0)) > 0.0
        stats["nav_contribution_pct"] += float(
            trade.get("nav_contribution_pct", 0.0)
        )
        exit_events = trade.get("exit_events") or []
        exit_date = (
            exit_events[-1].get("date") if exit_events else trade.get("exit_date")
        )
        if trade.get("entry_date") and exit_date:
            holding_days.append(
                (pd.Timestamp(exit_date) - pd.Timestamp(trade["entry_date"])).days
            )
    for stats in country_stats.values():
        stats["hit_rate_pct"] = (
            stats.pop("wins") / stats["trade_count"] * 100.0
        )
    return {
        "window": window_name,
        "variant": variant,
        "roi_pct": strategy.get("roi_pct"),
        "cagr_pct": strategy.get("cagr_pct"),
        "mdd_pct": strategy.get("mdd_pct"),
        "sharpe": strategy.get("sharpe"),
        "cvar_5pct_daily": cvar,
        "acwi_roi_pct": market.get("roi_pct"),
        "turnover_to_average_nav": turnover / average_nav if average_nav > 0 else None,
        "trade_count": result.get("all", {}).get("count", 0),
        "hit_rate_pct": result.get("all", {}).get("win_rate", 0.0),
        "average_holding_calendar_days": (
            sum(holding_days) / len(holding_days) if holding_days else None
        ),
        "country_stats": country_stats,
        "filled_trades": ledger.get("filled_trades"),
    }


def run_experiment(*, group="CSQ_UNIV", estimate_only=False, confirm_long_run=False):
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
        "ticker_backtests": len(universe) * len(VARIANTS) * 2,
        "external_downloads": 0,
        "variants": list(VARIANTS),
    }
    if estimate_only:
        return {"estimate": estimate}
    if not confirm_long_run:
        raise RuntimeError("long comparison requires --confirm-long-run")

    metadata = core_market_data_store.get_security_metadata(universe)
    summaries = []
    windows = {
        "IS": core_backtest_periods.selection_window(),
        "OOS": core_backtest_periods.oos_window(),
    }
    for variant, policy in VARIANTS.items():
        overlay = reversion.build_brain_alpha_trade_overlay(policy)
        for window_name, window in windows.items():
            result = core_universe_backtest.aggregate_universe_backtest(
                universe,
                reversion.run_reversion_backtest,
                neutralization="NONE",
                portfolio_accounting=True,
                signal_score_overlay=overlay,
                period_days=HISTORY_DAYS,
                **window,
            )
            summaries.append(_summary(window_name, variant, result, metadata))
    return {"estimate": estimate, "summaries": summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="CSQ_UNIV")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--confirm-long-run", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = run_experiment(
        group=args.group,
        estimate_only=args.estimate_only,
        confirm_long_run=args.confirm_long_run,
    )
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        path = Path(args.output)
        if path.exists():
            raise FileExistsError(f"output already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
