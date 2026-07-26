"""Portfolio-ledger NAV and same-universe benchmark calculations."""
import math

import pandas as pd

import core_market_data_store
from core_portfolio_allocator import PortfolioPolicy
from core_portfolio_ledger import simulate_portfolio


TRADING_DAYS_PER_YEAR = 252


def evaluate_return_target(
    strategy_return_pct,
    benchmark_return_pct,
    win_rate_pct,
    benchmark_multiple=2.0,
    win_rate_goal_pct=58.0,
):
    """Evaluate the ROI-first developer target; win rate is diagnostic only."""
    has_returns = (
        strategy_return_pct is not None
        and benchmark_return_pct is not None
        and math.isfinite(float(strategy_return_pct))
        and math.isfinite(float(benchmark_return_pct))
    )
    target_applicable = has_returns and float(benchmark_return_pct) > 0.0
    required_return = (
        float(benchmark_return_pct) * float(benchmark_multiple)
        if target_applicable
        else None
    )
    passes_return = (
        float(strategy_return_pct) >= required_return
        if target_applicable
        else None
    )
    meets_win_rate_goal = (
        win_rate_pct is not None
        and math.isfinite(float(win_rate_pct))
        and float(win_rate_pct) >= float(win_rate_goal_pct)
    )
    status = "REVIEW" if not target_applicable else ("PASS" if passes_return else "FAIL")
    return {
        "benchmark_multiple": float(benchmark_multiple),
        "win_rate_goal_pct": float(win_rate_goal_pct),
        "return_target_applicable": target_applicable,
        "required_return_pct": required_return,
        "passes_return_target": passes_return,
        "meets_win_rate_goal": meets_win_rate_goal,
        "win_rate_is_hard_gate": False,
        "status": status,
    }


def _pnl_decimal(side, price, entry_price):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (float(price) / float(entry_price) - 1.0)


def _trade_events(trade):
    events = [dict(event) for event in (trade.get("exit_events") or [])]
    if not events and trade.get("exit_date") and trade.get("exit_price") is not None:
        events = [
            {
                "date": trade["exit_date"],
                "weight": 1.0,
                "price": trade["exit_price"],
                "reason": "final exit",
            }
        ]
    return sorted(events, key=lambda event: event["date"])


def _calendar_and_closes(
    universe_list, start_date, end_date, price_loader, max_stale_calendar_days
):
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    closes = {}
    for ticker in sorted(set(universe_list)):
        prices = price_loader(ticker)
        if prices is None or prices.empty or "Close" not in prices:
            continue
        series = pd.to_numeric(prices["Close"], errors="coerce").dropna()
        series.index = pd.to_datetime(series.index).normalize()
        closes[ticker] = series[~series.index.duplicated(keep="last")].sort_index()
    if not closes:
        raise ValueError("No benchmark price data is available")

    evaluation_dates = sorted(
        {
            date
            for series in closes.values()
            for date in series.index
            if start <= date <= end
        }
    )
    if not evaluation_dates:
        raise ValueError("No price dates are available in the evaluation period")

    # Seed each constituent from its own last observation before the evaluation
    # window. A single global seed date drops valid prior closes when exchanges
    # have different holiday calendars.
    seed_dates = {
        prior[-1]
        for series in closes.values()
        if len(prior := series.index[series.index < start])
    }
    calendar = sorted(seed_dates | set(evaluation_dates))
    observed = pd.DataFrame(closes).reindex(calendar)
    close_frame = observed.ffill()
    for ticker in close_frame:
        last_seen = pd.Series(pd.NaT, index=close_frame.index, dtype="datetime64[ns]")
        available = observed[ticker].notna()
        last_seen.loc[available] = close_frame.index[available]
        last_seen = last_seen.ffill()
        age_days = (pd.Series(close_frame.index, index=close_frame.index) - last_seen).dt.days
        close_frame.loc[age_days > max_stale_calendar_days, ticker] = float("nan")
    return pd.DatetimeIndex(evaluation_dates), close_frame


def _trade_daily_returns(trade, calendar, close_frame):
    required = ("ticker", "side", "entry_date", "entry_price")
    missing = [field for field in required if trade.get(field) is None]
    if missing:
        raise ValueError(f"Trade is missing required fields: {', '.join(missing)}")

    ticker = trade["ticker"]
    if ticker not in close_frame:
        raise ValueError(f"No price data for traded ticker: {ticker}")
    entry_date = pd.Timestamp(trade["entry_date"])
    events = _trade_events(trade)
    if not events:
        raise ValueError(f"Trade has no exit event: {ticker} {trade['entry_date']}")
    final_date = pd.Timestamp(events[-1]["date"])
    events_by_date = {}
    for event in events:
        events_by_date.setdefault(pd.Timestamp(event["date"]), []).append(event)

    remaining = 1.0
    realized_pnl = 0.0
    previous_value = 1.0
    returns = {}
    for date in calendar:
        if date < entry_date or date > final_date:
            continue
        close = close_frame.at[date, ticker]
        if pd.isna(close):
            raise ValueError(f"Missing close for active trade: {ticker} {date.date()}")
        for event in events_by_date.get(date, []):
            weight = float(event["weight"])
            if weight < -1e-12 or weight > remaining + 1e-9:
                raise ValueError(f"Invalid exit weight for {ticker} on {date.date()}")
            realized_pnl += weight * _pnl_decimal(
                trade["side"], event["price"], trade["entry_price"]
            )
            remaining -= weight
        remaining = max(0.0, remaining)
        value = 1.0 + realized_pnl + remaining * _pnl_decimal(
            trade["side"], close, trade["entry_price"]
        )
        returns[date] = value / previous_value - 1.0
        previous_value = value
    if remaining > 1e-8:
        raise ValueError(f"Trade is not fully exited: {ticker} {trade['entry_date']}")
    return returns


def _summary(returns, nav, initial_value=1.0):
    count = len(returns)
    initial_value = float(initial_value)
    roi = float(nav.iloc[-1] / initial_value - 1.0) if count else 0.0
    cagr = (
        float(
            (nav.iloc[-1] / initial_value)
            ** (TRADING_DAYS_PER_YEAR / count)
            - 1.0
        )
        if count and nav.iloc[-1] > 0
        else None
    )
    # Initial capital is part of the high-water mark, including on day one.
    high_water = nav.cummax().clip(lower=initial_value)
    drawdown = nav / high_water - 1.0
    volatility = float(returns.std(ddof=1)) if count > 1 else 0.0
    sharpe = (
        float(returns.mean() / volatility * math.sqrt(TRADING_DAYS_PER_YEAR))
        if volatility > 0
        else None
    )
    return {
        "roi_pct": roi * 100.0,
        "cagr_pct": None if cagr is None else cagr * 100.0,
        "mdd_pct": float(drawdown.min()) * 100.0,
        "sharpe": sharpe,
    }


def calculate_equal_weight_performance(
    trades,
    universe_list,
    start_date,
    end_date,
    price_loader=core_market_data_store.get_price_data,
    max_stale_calendar_days=7,
):
    """Return daily NAV plus strategy and benchmark performance summaries.

    Each active trade is a sleeve. Sleeves are rebalanced to equal portfolio
    weight daily; cash created by a partial exit remains inside that sleeve.
    The benchmark is rebalanced daily across the same universe using raw Close.
    """
    calendar, close_frame = _calendar_and_closes(
        universe_list,
        start_date,
        end_date,
        price_loader,
        max_stale_calendar_days,
    )
    contributions = {date: [] for date in calendar}
    for trade in trades:
        for date, value in _trade_daily_returns(trade, calendar, close_frame).items():
            contributions[date].append(value)

    strategy_returns = pd.Series(
        [sum(values) / len(values) if values else 0.0 for values in contributions.values()],
        index=calendar,
        dtype=float,
    )
    active_positions = pd.Series(
        [len(values) for values in contributions.values()], index=calendar, dtype=int
    )

    benchmark_returns = close_frame.pct_change(fill_method=None).reindex(calendar)
    benchmark_constituents = benchmark_returns.notna().sum(axis=1)
    benchmark_returns = benchmark_returns.mean(axis=1, skipna=True).fillna(0.0)
    strategy_nav = (1.0 + strategy_returns).cumprod()
    benchmark_nav = (1.0 + benchmark_returns).cumprod()
    daily = pd.DataFrame(
        {
            "strategy_return": strategy_returns,
            "strategy_nav": strategy_nav,
            "benchmark_return": benchmark_returns,
            "benchmark_nav": benchmark_nav,
            "active_positions": active_positions,
            "benchmark_constituents": benchmark_constituents,
        }
    )
    strategy_summary = _summary(strategy_returns, strategy_nav)
    benchmark_summary = _summary(benchmark_returns, benchmark_nav)
    strategy_cagr = strategy_summary["cagr_pct"]
    benchmark_cagr = benchmark_summary["cagr_pct"]
    return {
        "daily": daily,
        "strategy": strategy_summary,
        "benchmark": benchmark_summary,
        "excess_cagr_pct": (
            None
            if strategy_cagr is None or benchmark_cagr is None
            else strategy_cagr - benchmark_cagr
        ),
        "period": f"{calendar[0].date()} to {calendar[-1].date()}",
        "trading_days": len(calendar),
        "benchmark_tickers": len(close_frame.columns),
        "benchmark_min_daily_tickers": int(benchmark_constituents.min()),
        "benchmark_max_daily_tickers": int(benchmark_constituents.max()),
    }


def calculate_score_weighted_performance(
    trades,
    universe_list,
    start_date,
    end_date,
    price_loader=core_market_data_store.get_price_data,
    position_mode="LONG_SHORT_NEUTRAL",
    policy=None,
    max_stale_calendar_days=7,
):
    """Replay actual quantities and cash, then compare with the same universe.

    Entry weights are requested from the score pipeline and constrained at the
    open. Quantities remain fixed until the strategy emits a partial/full exit.
    """
    calendar, close_frame = _calendar_and_closes(
        universe_list,
        start_date,
        end_date,
        price_loader,
        max_stale_calendar_days,
    )
    if policy is None:
        policy = PortfolioPolicy(position_mode=position_mode)
    ledger = simulate_portfolio(
        trades,
        start_date,
        end_date,
        price_loader=price_loader,
        policy=policy,
        calendar=calendar,
    )
    ledger_daily = ledger["daily"].reindex(calendar)
    strategy_returns = ledger_daily["portfolio_return"].fillna(0.0)
    strategy_nav = ledger_daily["portfolio_nav"] / float(policy.initial_nav)

    benchmark_returns = close_frame.pct_change(fill_method=None).reindex(calendar)
    benchmark_constituents = benchmark_returns.notna().sum(axis=1)
    benchmark_returns = benchmark_returns.mean(axis=1, skipna=True).fillna(0.0)
    benchmark_nav = (1.0 + benchmark_returns).cumprod()

    daily = ledger_daily.rename(
        columns={
            "portfolio_return": "strategy_return",
            "portfolio_nav": "portfolio_value",
        }
    ).copy()
    daily["strategy_nav"] = strategy_nav
    daily["benchmark_return"] = benchmark_returns
    daily["benchmark_nav"] = benchmark_nav
    daily["benchmark_constituents"] = benchmark_constituents

    strategy_summary = _summary(strategy_returns, strategy_nav)
    benchmark_summary = _summary(benchmark_returns, benchmark_nav)
    strategy_cagr = strategy_summary["cagr_pct"]
    benchmark_cagr = benchmark_summary["cagr_pct"]
    return {
        "daily": daily,
        "strategy": strategy_summary,
        "benchmark": benchmark_summary,
        "excess_cagr_pct": (
            None
            if strategy_cagr is None or benchmark_cagr is None
            else strategy_cagr - benchmark_cagr
        ),
        "period": f"{calendar[0].date()} to {calendar[-1].date()}",
        "trading_days": len(calendar),
        "benchmark_tickers": len(close_frame.columns),
        "benchmark_min_daily_tickers": int(benchmark_constituents.min()),
        "benchmark_max_daily_tickers": int(benchmark_constituents.max()),
        "ledger": {
            "trades": ledger["trades"],
            "events": ledger["events"],
            "filled_trades": ledger["filled_trades"],
            "skipped_trades": ledger["skipped_trades"],
            "policy": {
                "initial_nav": policy.initial_nav,
                "max_gross": policy.max_gross,
                "max_name_weight": policy.max_name_weight,
                "long_cap": policy.side_caps[0],
                "short_cap": policy.side_caps[1],
                "position_mode": policy.position_mode,
            },
        },
    }


def calculate_universe_performance(
    universe_result,
    universe_list,
    price_loader=core_market_data_store.get_price_data,
    benchmark_multiple=2.0,
    minimum_win_rate_pct=58.0,
    max_stale_calendar_days=7,
    position_mode="LONG_SHORT_NEUTRAL",
    policy=None,
):
    """Calculate developer-only portfolio metrics from an aggregate backtest."""
    period = universe_result.get("period", "N/A")
    if " to " not in period:
        raise ValueError("Universe result has no valid evaluation period")
    start_date, end_date = period.split(" to ", 1)
    result = calculate_score_weighted_performance(
        universe_result.get("trades", []),
        universe_list,
        start_date,
        end_date,
        price_loader=price_loader,
        position_mode=position_mode,
        policy=policy,
        max_stale_calendar_days=max_stale_calendar_days,
    )
    executed_trades = [
        trade
        for trade in result["ledger"]["trades"]
        if trade.get("allocation_status") == "FILLED"
    ]
    trade_returns = [
        float(trade["return"])
        for trade in executed_trades
        if trade.get("return") is not None
    ]
    if len(trade_returns) == len(executed_trades):
        trade_count = len(trade_returns)
        win_rate = (
            sum(value > 0 for value in trade_returns) / trade_count * 100.0
            if trade_count
            else 0.0
        )
    else:
        trade_count = int(universe_result.get("all", {}).get("count", 0))
        win_rate = float(
            universe_result.get("all", {}).get("win_rate", 0.0)
        )
    target = evaluate_return_target(
        result["strategy"]["roi_pct"],
        result["benchmark"]["roi_pct"],
        win_rate,
        benchmark_multiple=benchmark_multiple,
        win_rate_goal_pct=minimum_win_rate_pct,
    )
    result.update(
        {
            "trade_count": trade_count,
            "win_rate_pct": win_rate,
            "target": {
                "benchmark_multiple": benchmark_multiple,
                "minimum_win_rate_pct": minimum_win_rate_pct,
                "primary_metric": "1Y cumulative ROI",
                "required_roi_pct": target["required_return_pct"],
                "passes_roi": target["passes_return_target"],
                "passes_primary_return_target": target["passes_return_target"],
                "required_cagr_pct": None,
                "passes_cagr": None,
                "passes_win_rate": target["meets_win_rate_goal"],
                "win_rate_is_hard_gate": False,
                "status": target["status"],
                # Backward-compatible key: now means the primary ROI target only.
                "passes_all": target["passes_return_target"] is True,
            },
        }
    )
    return result
