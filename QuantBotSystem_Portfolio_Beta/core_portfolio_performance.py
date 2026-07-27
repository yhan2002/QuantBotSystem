"""Portfolio-ledger NAV and market/universe benchmark calculations."""
import math

import pandas as pd

import core_backtest_periods
import core_market_data_store
from core_portfolio_allocator import PortfolioPolicy
from core_portfolio_ledger import simulate_portfolio


TRADING_DAYS_PER_YEAR = 252
MARKET_BENCHMARK_TICKER = "ACWI"
MARKET_BENCHMARK_LABEL = "ACWI"
BENCHMARK_BEAT_MULTIPLE = 1.0


def evaluate_return_target(
    strategy_return_pct,
    benchmark_return_pct,
    win_rate_pct,
    benchmark_multiple=BENCHMARK_BEAT_MULTIPLE,
    win_rate_goal_pct=58.0,
):
    """Evaluate the ROI-first target; the strategy must strictly beat it."""
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
        float(strategy_return_pct) > required_return
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
        "comparison_operator": ">",
        "passes_benchmark_roi": passes_return,
        # Backward-compatible generic alias.
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
    if remaining > 1e-8 and final_date <= calendar[-1]:
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


def _unavailable_summary():
    return {
        "roi_pct": None,
        "cagr_pct": None,
        "mdd_pct": None,
        "sharpe": None,
    }


def _difference(left, right):
    if left is None or right is None:
        return None
    return float(left) - float(right)


def _universe_equal_weight_benchmark(close_frame, calendar):
    returns_by_ticker = close_frame.pct_change(fill_method=None).reindex(calendar)
    constituents = returns_by_ticker.notna().sum(axis=1)
    returns = returns_by_ticker.mean(axis=1, skipna=True).fillna(0.0)
    nav = (1.0 + returns).cumprod()
    return returns, nav, constituents, _summary(returns, nav)


def _market_benchmark(
    calendar,
    price_loader,
    ticker,
    max_stale_calendar_days,
):
    empty = pd.Series(float("nan"), index=calendar, dtype=float)
    unavailable = {
        "available": False,
        "reason": f"No local {ticker} data",
        "returns": empty.copy(),
        "nav": empty.copy(),
        "summary": _unavailable_summary(),
        "coverage_pct": 0.0,
        "first_date": None,
        "latest_date": None,
        "start_level_date": None,
        "end_level_date": None,
        "start_level": None,
        "end_level": None,
        "daily_curve_available": False,
        "daily_curve_reason": "Benchmark endpoints are unavailable",
    }
    try:
        prices = price_loader(ticker)
    except Exception as exc:
        unavailable["reason"] = f"{ticker} load failed: {exc}"
        return unavailable
    if prices is None or prices.empty:
        return unavailable

    start = calendar[0]
    end = calendar[-1]
    price_column = None
    series = pd.Series(dtype=float)
    candidate_reasons = []
    for candidate in ("Adj Close", "Close"):
        if candidate not in prices:
            continue
        candidate_series = pd.to_numeric(prices[candidate], errors="coerce").dropna()
        candidate_series = candidate_series[
            (candidate_series > 0) & candidate_series.map(math.isfinite)
        ]
        if candidate_series.empty:
            candidate_reasons.append(f"{candidate} has no valid values")
            continue
        candidate_series.index = pd.to_datetime(candidate_series.index).normalize()
        candidate_series = candidate_series[
            ~candidate_series.index.duplicated(keep="last")
        ].sort_index()
        prior = candidate_series.index[candidate_series.index < start]
        end_candidates = candidate_series.index[candidate_series.index <= end]
        if prior.empty:
            candidate_reasons.append(f"{candidate} has no close before {start.date()}")
            continue
        if end_candidates.empty:
            candidate_reasons.append(
                f"{candidate} has no close on or before {end.date()}"
            )
            continue
        seed_date = prior[-1]
        end_level_date = end_candidates[-1]
        if (start - seed_date).days > max_stale_calendar_days:
            candidate_reasons.append(
                f"{candidate} start level is {(start - seed_date).days} days stale"
            )
            continue
        if (end - end_level_date).days > max_stale_calendar_days:
            candidate_reasons.append(
                f"{candidate} end level is {(end - end_level_date).days} days stale"
            )
            continue
        price_column = candidate
        series = candidate_series
        break
    if price_column is None:
        if candidate_reasons:
            unavailable["reason"] = f"{ticker}: " + "; ".join(candidate_reasons)
        return unavailable

    prior = series.index[series.index < start]
    end_candidates = series.index[series.index <= end]
    observed_in_window = series.loc[(series.index >= start) & (series.index <= end)]
    unavailable["first_date"] = series.index[0].date().isoformat()
    unavailable["latest_date"] = series.index[-1].date().isoformat()
    if prior.empty:
        unavailable["reason"] = f"{ticker} has no close before {start.date()}"
        return unavailable
    if end_candidates.empty:
        unavailable["reason"] = f"{ticker} has no close on or before {end.date()}"
        return unavailable

    seed_date = prior[-1]
    end_level_date = end_candidates[-1]
    if (start - seed_date).days > max_stale_calendar_days:
        unavailable["reason"] = (
            f"{ticker} start level is {(start - seed_date).days} days stale"
        )
        return unavailable
    if (end - end_level_date).days > max_stale_calendar_days:
        unavailable["reason"] = (
            f"{ticker} end level is {(end - end_level_date).days} days stale"
        )
        return unavailable

    start_level = float(series.loc[seed_date])
    end_level = float(series.loc[end_level_date])
    endpoint_roi = end_level / start_level - 1.0
    extended_index = pd.DatetimeIndex(
        sorted(set(calendar) | {seed_date} | set(observed_in_window.index))
    )
    observed = series.reindex(extended_index)
    filled = observed.ffill()
    last_seen = pd.Series(pd.NaT, index=extended_index, dtype="datetime64[ns]")
    available_rows = observed.notna()
    last_seen.loc[available_rows] = extended_index[available_rows]
    last_seen = last_seen.ffill()
    age_days = (
        pd.Series(extended_index, index=extended_index) - last_seen
    ).dt.days
    filled.loc[age_days > max_stale_calendar_days] = float("nan")
    # Compute returns after aligning levels to the evaluation calendar. This
    # carries any ACWI move on a universe-wide holiday into the next evaluation
    # date instead of dropping the off-calendar return.
    aligned_index = pd.DatetimeIndex([seed_date, *calendar])
    aligned_levels = filled.reindex(aligned_index)
    returns = aligned_levels.pct_change(fill_method=None).reindex(calendar)
    valid = returns.notna()
    coverage_pct = float(valid.mean() * 100.0) if len(valid) else 0.0
    daily_curve_available = bool(valid.all())
    if daily_curve_available:
        nav = (1.0 + returns).cumprod()
        summary = _summary(returns, nav)
        # Boundary levels are the authoritative market-comparison definition.
        summary["roi_pct"] = endpoint_roi * 100.0
        daily_curve_reason = ""
    else:
        missing = int((~valid).sum())
        returns = empty.copy()
        nav = empty.copy()
        summary = {
            "roi_pct": endpoint_roi * 100.0,
            "cagr_pct": None,
            "mdd_pct": None,
            "sharpe": None,
        }
        daily_curve_reason = (
            f"{ticker} daily curve missing {missing}/{len(calendar)} dates; "
            "ROI uses boundary levels only"
        )
    return {
        "available": True,
        "reason": "",
        "returns": returns,
        "nav": nav,
        "summary": summary,
        "coverage_pct": coverage_pct,
        "first_date": unavailable["first_date"],
        "latest_date": unavailable["latest_date"],
        "start_level_date": seed_date.date().isoformat(),
        "end_level_date": end_level_date.date().isoformat(),
        "start_level": start_level,
        "end_level": end_level,
        "daily_curve_available": daily_curve_available,
        "daily_curve_reason": daily_curve_reason,
        "price_column": price_column,
    }


def _align_calendar_to_market_end(
    calendar,
    close_frame,
    price_loader,
    ticker,
    max_stale_calendar_days,
):
    """Limit strategy metrics to the latest date shared with the benchmark."""
    market = _market_benchmark(
        calendar,
        price_loader,
        ticker,
        max_stale_calendar_days,
    )
    if not market["available"] or not market.get("end_level_date"):
        return calendar, close_frame
    common_end = pd.Timestamp(market["end_level_date"]).normalize()
    common_calendar = calendar[calendar <= common_end]
    if common_calendar.empty or len(common_calendar) == len(calendar):
        return calendar, close_frame
    return common_calendar, close_frame.reindex(common_calendar)


def _prepare_trades_for_evaluation(trades, calendar):
    """Replace exits after the common benchmark date with an explicit close mark."""
    evaluation_end = pd.Timestamp(calendar[-1]).normalize()
    prepared = []
    for raw_trade in trades:
        entry_date = pd.Timestamp(raw_trade["entry_date"]).normalize()
        if entry_date > evaluation_end:
            continue
        events = _trade_events(raw_trade)
        if not events:
            raise ValueError(
                f"Trade has no exit event: {raw_trade.get('ticker')} "
                f"{raw_trade.get('entry_date')}"
            )

        retained_events = []
        remaining = 1.0
        for raw_event in events:
            event = dict(raw_event)
            event_date = pd.Timestamp(event["date"]).normalize()
            if event_date > evaluation_end:
                continue
            weight = float(event.get("weight", 1.0))
            if weight < -1e-12 or weight > remaining + 1e-9:
                raise ValueError(
                    f"Invalid exit weight for {raw_trade.get('ticker')} "
                    f"on {event_date.date()}"
                )
            event["date"] = event_date.date().isoformat()
            event["weight"] = weight
            retained_events.append(event)
            remaining = max(0.0, remaining - weight)

        trade = dict(raw_trade)
        trade["exit_events"] = retained_events
        trade["evaluation_status"] = (
            "MARKED_AT_EVALUATION_END"
            if any(
                _is_evaluation_mark_reason(event.get("reason"))
                for event in retained_events
            )
            else "CLOSED"
        )
        if remaining > 1e-8:
            mark_date = evaluation_end.date().isoformat()
            trade["original_exit_date"] = (
                raw_trade.get("exit_date")
                or pd.Timestamp(events[-1]["date"]).date().isoformat()
            )
            trade["original_return"] = raw_trade.get("return")
            trade["exit_date"] = mark_date
            trade["exit_reason"] = "evaluation-end mark"
            trade.pop("exit_price", None)
            trade["evaluation_status"] = "MARKED_AT_EVALUATION_END"
            trade["exit_events"].append(
                {
                    "date": mark_date,
                    "weight": remaining,
                    "reason": "evaluation-end mark",
                    "execution": "CLOSE",
                    "evaluation_mark": True,
                }
            )
        prepared.append(trade)
    return prepared


def _is_evaluation_mark_reason(reason):
    normalized = str(reason or "").lower().replace("-", " ").replace("_", " ")
    return " ".join(normalized.split()) == "evaluation end mark"


def _annotate_evaluated_trade_returns(ledger):
    mark_events = {}
    for event in ledger.get("events", []):
        if (
            event.get("event") == "EXIT"
            and _is_evaluation_mark_reason(event.get("reason"))
        ):
            mark_events.setdefault(event["trade_id"], []).append(event)

    for trade in ledger.get("trades", []):
        if trade.get("allocation_status") != "FILLED":
            continue
        allocated_base = float(trade.get("allocated_base_notional") or 0.0)
        realized_pnl = float(trade.get("realized_pnl") or 0.0)
        evaluated_return = (
            realized_pnl / allocated_base * 100.0
            if allocated_base > 0
            else None
        )
        trade["evaluated_return_pct"] = evaluated_return
        if (
            evaluated_return is not None
            and trade.get("evaluation_status") == "MARKED_AT_EVALUATION_END"
        ):
            trade["return"] = evaluated_return

        events = mark_events.get(trade.get("trade_id"), [])
        if not events:
            continue
        trade["evaluation_mark_date"] = str(events[0]["date"].date())
        trade["evaluation_mark_leg_prices"] = {
            event["ticker"]: float(event["price"])
            for event in events
        }
        if len(events) == 1:
            trade["evaluation_mark_price"] = float(events[0]["price"])
            trade["exit_price"] = float(events[0]["price"])
        for exit_event in trade.get("exit_events") or []:
            if exit_event.get("evaluation_mark"):
                exit_event["leg_prices"] = dict(
                    trade["evaluation_mark_leg_prices"]
                )
                if len(events) == 1:
                    exit_event["price"] = float(events[0]["price"])


def _benchmark_payload(
    calendar,
    close_frame,
    market_benchmark_loader,
    market_benchmark_ticker,
    market_benchmark_label,
    max_stale_calendar_days,
):
    (
        universe_returns,
        universe_nav,
        universe_constituents,
        universe_summary,
    ) = _universe_equal_weight_benchmark(close_frame, calendar)
    market = _market_benchmark(
        calendar,
        market_benchmark_loader,
        market_benchmark_ticker,
        max_stale_calendar_days,
    )
    return {
        "market": market,
        "market_label": market_benchmark_label,
        "market_ticker": market_benchmark_ticker,
        "universe_returns": universe_returns,
        "universe_nav": universe_nav,
        "universe_constituents": universe_constituents,
        "universe_summary": universe_summary,
    }


def _add_benchmark_columns(daily, benchmarks):
    market = benchmarks["market"]
    daily["market_benchmark_return"] = market["returns"]
    daily["market_benchmark_nav"] = market["nav"]
    daily["universe_ew_return"] = benchmarks["universe_returns"]
    daily["universe_ew_nav"] = benchmarks["universe_nav"]
    daily["universe_ew_constituents"] = benchmarks["universe_constituents"]
    # Deprecated aliases retain their historical Universe EW meaning.
    daily["benchmark_return"] = benchmarks["universe_returns"]
    daily["benchmark_nav"] = benchmarks["universe_nav"]
    daily["benchmark_constituents"] = benchmarks["universe_constituents"]
    return daily


def _performance_payload(strategy_summary, daily, calendar, close_frame, benchmarks):
    market = benchmarks["market"]
    market_summary = market["summary"]
    universe_summary = benchmarks["universe_summary"]
    strategy_roi = strategy_summary["roi_pct"]
    market_roi = market_summary["roi_pct"]
    universe_roi = universe_summary["roi_pct"]
    market_multiple = (
        strategy_roi / market_roi
        if strategy_roi is not None and market_roi is not None and market_roi > 0
        else None
    )
    constituents = benchmarks["universe_constituents"]
    return {
        "daily": daily,
        "strategy": strategy_summary,
        # Deprecated alias retained for existing CSV consumers.
        "benchmark": universe_summary,
        "market_benchmark": market_summary,
        "market_benchmark_label": benchmarks["market_label"],
        "market_benchmark_ticker": benchmarks["market_ticker"],
        "market_benchmark_available": market["available"],
        "market_benchmark_unavailable_reason": market["reason"],
        "market_benchmark_coverage_pct": market["coverage_pct"],
        "market_benchmark_first_date": market["first_date"],
        "market_benchmark_latest_date": market["latest_date"],
        "market_benchmark_start_level_date": market["start_level_date"],
        "market_benchmark_end_level_date": market["end_level_date"],
        "market_benchmark_start_level": market["start_level"],
        "market_benchmark_end_level": market["end_level"],
        "market_benchmark_daily_curve_available": market["daily_curve_available"],
        "market_benchmark_daily_curve_reason": market["daily_curve_reason"],
        "market_benchmark_price_column": market.get("price_column"),
        "market_benchmark_multiple": market_multiple,
        "universe_ew_benchmark": universe_summary,
        "market_excess_roi_pct": _difference(strategy_roi, market_roi),
        "stock_selection_excess_pct": _difference(strategy_roi, universe_roi),
        "market_excess_cagr_pct": _difference(
            strategy_summary["cagr_pct"], market_summary["cagr_pct"]
        ),
        # Deprecated alias retains its historical Universe EW meaning.
        "excess_cagr_pct": _difference(
            strategy_summary["cagr_pct"], universe_summary["cagr_pct"]
        ),
        "period": f"{calendar[0].date()} to {calendar[-1].date()}",
        "trading_days": len(calendar),
        "universe_ew_tickers": len(close_frame.columns),
        "universe_ew_min_daily_tickers": int(constituents.min()),
        "universe_ew_max_daily_tickers": int(constituents.max()),
        # Deprecated aliases retained for existing callers.
        "benchmark_tickers": len(close_frame.columns),
        "benchmark_min_daily_tickers": int(constituents.min()),
        "benchmark_max_daily_tickers": int(constituents.max()),
    }


def calculate_equal_weight_performance(
    trades,
    universe_list,
    start_date,
    end_date,
    price_loader=core_market_data_store.get_price_data,
    max_stale_calendar_days=core_backtest_periods.BENCHMARK_MAX_STALE_CALENDAR_DAYS,
    market_benchmark_loader=None,
    market_benchmark_ticker=MARKET_BENCHMARK_TICKER,
    market_benchmark_label=MARKET_BENCHMARK_LABEL,
):
    """Return daily NAV plus market and universe benchmark summaries.

    Each active trade is a sleeve. Sleeves are rebalanced to equal portfolio
    weight daily; cash created by a partial exit remains inside that sleeve.
    ACWI is the primary target benchmark. The daily equal-weight raw-close
    universe is retained as a stock-selection diagnostic.
    """
    calendar, close_frame = _calendar_and_closes(
        universe_list,
        start_date,
        end_date,
        price_loader,
        max_stale_calendar_days,
    )
    benchmark_loader = market_benchmark_loader or price_loader
    calendar, close_frame = _align_calendar_to_market_end(
        calendar,
        close_frame,
        benchmark_loader,
        market_benchmark_ticker,
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

    strategy_nav = (1.0 + strategy_returns).cumprod()
    daily = pd.DataFrame(
        {
            "strategy_return": strategy_returns,
            "strategy_nav": strategy_nav,
            "active_positions": active_positions,
        }
    )
    benchmarks = _benchmark_payload(
        calendar,
        close_frame,
        benchmark_loader,
        market_benchmark_ticker,
        market_benchmark_label,
        max_stale_calendar_days,
    )
    daily = _add_benchmark_columns(daily, benchmarks)
    strategy_summary = _summary(strategy_returns, strategy_nav)
    return _performance_payload(
        strategy_summary,
        daily,
        calendar,
        close_frame,
        benchmarks,
    )


def calculate_score_weighted_performance(
    trades,
    universe_list,
    start_date,
    end_date,
    price_loader=core_market_data_store.get_price_data,
    position_mode="LONG_SHORT_NEUTRAL",
    policy=None,
    max_stale_calendar_days=core_backtest_periods.BENCHMARK_MAX_STALE_CALENDAR_DAYS,
    market_benchmark_loader=None,
    market_benchmark_ticker=MARKET_BENCHMARK_TICKER,
    market_benchmark_label=MARKET_BENCHMARK_LABEL,
):
    """Replay actual quantities and cash, then compare with ACWI and Universe EW.

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
    benchmark_loader = market_benchmark_loader or price_loader
    calendar, close_frame = _align_calendar_to_market_end(
        calendar,
        close_frame,
        benchmark_loader,
        market_benchmark_ticker,
        max_stale_calendar_days,
    )
    if policy is None:
        policy = PortfolioPolicy(position_mode=position_mode)
    evaluation_trades = _prepare_trades_for_evaluation(trades, calendar)
    ledger = simulate_portfolio(
        evaluation_trades,
        start_date,
        end_date,
        price_loader=price_loader,
        policy=policy,
        calendar=calendar,
    )
    _annotate_evaluated_trade_returns(ledger)
    ledger_daily = ledger["daily"].reindex(calendar)
    strategy_returns = ledger_daily["portfolio_return"].fillna(0.0)
    strategy_nav = ledger_daily["portfolio_nav"] / float(policy.initial_nav)

    daily = ledger_daily.rename(
        columns={
            "portfolio_return": "strategy_return",
            "portfolio_nav": "portfolio_value",
        }
    ).copy()
    daily["strategy_nav"] = strategy_nav
    benchmarks = _benchmark_payload(
        calendar,
        close_frame,
        benchmark_loader,
        market_benchmark_ticker,
        market_benchmark_label,
        max_stale_calendar_days,
    )
    daily = _add_benchmark_columns(daily, benchmarks)

    strategy_summary = _summary(strategy_returns, strategy_nav)
    result = _performance_payload(
        strategy_summary,
        daily,
        calendar,
        close_frame,
        benchmarks,
    )
    result["ledger"] = {
        "trades": ledger["trades"],
        "events": ledger["events"],
        "filled_trades": ledger["filled_trades"],
        "skipped_trades": ledger["skipped_trades"],
        "evaluation_end_marks": sum(
            trade.get("allocation_status") == "FILLED"
            and trade.get("evaluation_status") == "MARKED_AT_EVALUATION_END"
            for trade in ledger["trades"]
        ),
        "policy": {
            "initial_nav": policy.initial_nav,
            "max_gross": policy.max_gross,
            "max_name_weight": policy.max_name_weight,
            "long_cap": policy.side_caps[0],
            "short_cap": policy.side_caps[1],
            "position_mode": policy.position_mode,
        },
    }
    return result


def calculate_universe_performance(
    universe_result,
    universe_list,
    price_loader=core_market_data_store.get_price_data,
    benchmark_multiple=BENCHMARK_BEAT_MULTIPLE,
    minimum_win_rate_pct=58.0,
    max_stale_calendar_days=core_backtest_periods.BENCHMARK_MAX_STALE_CALENDAR_DAYS,
    position_mode="LONG_SHORT_NEUTRAL",
    policy=None,
    market_benchmark_loader=None,
    market_benchmark_ticker=MARKET_BENCHMARK_TICKER,
    market_benchmark_label=MARKET_BENCHMARK_LABEL,
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
        market_benchmark_loader=market_benchmark_loader,
        market_benchmark_ticker=market_benchmark_ticker,
        market_benchmark_label=market_benchmark_label,
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
        result["market_benchmark"]["roi_pct"],
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
                "benchmark_label": result["market_benchmark_label"],
                "benchmark_ticker": result["market_benchmark_ticker"],
                "required_roi_pct": target["required_return_pct"],
                "comparison_operator": target["comparison_operator"],
                "passes_benchmark_roi": target["passes_benchmark_roi"],
                "passes_acwi_roi": target["passes_benchmark_roi"],
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
