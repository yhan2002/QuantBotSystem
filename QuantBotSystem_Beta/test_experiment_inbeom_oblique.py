"""Standalone LONG-only experiment for the Inbeom oblique-channel idea.

Trend construction uses weekly bars only.  It connects two confirmed major
highs in either regular-price or log-price space, copies that line through a
later major high to make a matching parallel channel, and buys a breakout only after a later
supporting pullback.  Daily data is used solely for next-trading-day opens.

This module is intentionally not connected to the production bot.
"""
import argparse
import math
from statistics import median

import pandas as pd

import core_market_data_store


HISTORY_YEARS = 10
MIN_HISTORY_WEEKS = 500
PIVOT_SPAN_WEEKS = 8
MIN_ANCHOR_SEPARATION_WEEKS = 52
MIN_CHANNEL_WIDTH_PCT = 0.08
RETEST_WINDOW_WEEKS = 26
TOUCH_TOLERANCE_PCT = 0.02
MAX_ENTRY_DISTANCE_PCT = 0.03
MAX_CHANNEL_LEVEL = 4
ATR_WEEKS = 14
MIN_RISK_REWARD = 0.0
CHANNEL_SCALES = ("linear", "log")


def _normalize_ohlcv(df):
    """Normalize daily OHLCV and adjust OHLC for splits before chart analysis."""
    required = ["Open", "High", "Low", "Close"]
    if df is None or df.empty or any(column not in df.columns for column in required):
        return pd.DataFrame()
    out = df.dropna(subset=required).copy().sort_index()
    if "Adj Close" in out.columns:
        adjustment = (out["Adj Close"] / out["Close"]).replace([float("inf"), -float("inf")], pd.NA)
        adjustment = adjustment.fillna(1.0)
        for column in required:
            out[column] = out[column] * adjustment
    return out


def weekly_ohlc(df):
    """Aggregate daily bars while retaining each week's actual final trading day."""
    rows = []
    for _, group in df.groupby(df.index.to_period("W-FRI"), sort=True):
        rows.append(
            {
                "signal_date": group.index[-1],
                "Open": float(group["Open"].iloc[0]),
                "High": float(group["High"].max()),
                "Low": float(group["Low"].min()),
                "Close": float(group["Close"].iloc[-1]),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("signal_date")


def weekly_atr(weekly, atr_weeks=ATR_WEEKS):
    """ATR uses only completed weekly bars available at the signal close."""
    previous_close = weekly["Close"].shift(1)
    true_range = pd.concat(
        [
            weekly["High"] - weekly["Low"],
            (weekly["High"] - previous_close).abs(),
            (weekly["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(atr_weeks, min_periods=atr_weeks).mean()


def confirmed_pivots(weekly, column, pivot_span=PIVOT_SPAN_WEEKS):
    """Return pivots only after the right-side weekly bars have completed."""
    pivots = []
    prices = weekly[column].reset_index(drop=True)
    for pivot_idx in range(pivot_span, len(prices) - pivot_span):
        window = prices.iloc[pivot_idx - pivot_span:pivot_idx + pivot_span + 1]
        price = float(prices.iloc[pivot_idx])
        is_extreme = price == (float(window.max()) if column == "High" else float(window.min()))
        if is_extreme and int((window == price).sum()) == 1:
            pivots.append(
                {
                    "pivot_idx": pivot_idx,
                    "confirmed_idx": pivot_idx + pivot_span,
                    "price": price,
                }
            )
    return pivots


def _line_from_points(first, second, channel_scale="linear"):
    if channel_scale == "log":
        slope = (math.log(second["price"]) - math.log(first["price"])) / (
            second["pivot_idx"] - first["pivot_idx"]
        )
    else:
        slope = (second["price"] - first["price"]) / (
            second["pivot_idx"] - first["pivot_idx"]
        )
    return {"first": first, "second": second, "slope": slope, "channel_scale": channel_scale}


def _line_value(line, idx, level=0):
    """Return a channel value using either regular-price or log-price geometry."""
    if line.get("channel_scale", "linear") == "log":
        return math.exp(
            math.log(line["second"]["price"])
            + line["slope"] * (idx - line["second"]["pivot_idx"])
            + level * line["spacing"]
        )
    return (
        line["second"]["price"]
        + line["slope"] * (idx - line["second"]["pivot_idx"])
        + level * line["spacing"]
    )


def latest_historical_high_channel(
    high_pivots,
    as_of_idx,
    min_anchor_separation_weeks=MIN_ANCHOR_SEPARATION_WEEKS,
    min_channel_width_pct=MIN_CHANNEL_WIDTH_PCT,
    channel_scale="linear",
):
    """Build the newest confirmed high-high channel available at ``as_of_idx``.

    The first two highs define the base line.  A later high above that line
    confirms the equal channel spacing.  All three pivots must already be
    confirmed, avoiding hindsight in the channel construction.
    """
    available = [pivot for pivot in high_pivots if pivot["confirmed_idx"] <= as_of_idx]
    if len(available) < 3:
        return None

    for third_position in range(len(available) - 1, 1, -1):
        third = available[third_position]
        for second_position in range(third_position - 1, 0, -1):
            second = available[second_position]
            for first_position in range(second_position - 1, -1, -1):
                first = available[first_position]
                if second["pivot_idx"] - first["pivot_idx"] < min_anchor_separation_weeks:
                    continue
                if second["price"] <= first["price"]:
                    continue
                line = _line_from_points(first, second, channel_scale=channel_scale)
                base_at_third = _line_value({**line, "spacing": 0.0}, third["pivot_idx"])
                if channel_scale == "log":
                    spacing = math.log(third["price"] / base_at_third)
                    width_pct = math.exp(spacing) - 1.0
                else:
                    spacing = third["price"] - base_at_third
                    width_pct = spacing / base_at_third
                if width_pct < min_channel_width_pct:
                    continue
                line["spacing"] = spacing
                line["third"] = third
                return line
    return None


def _next_daily_idx(df, signal_date):
    later = df.index[df.index > signal_date]
    if len(later) == 0:
        return None
    return int(df.index.get_loc(later[0]))


def _crossed_above_channel(weekly, line, idx, max_channel_level):
    for level in range(max_channel_level, 0, -1):
        previous_level = _line_value(line, idx - 1, level)
        current_level = _line_value(line, idx, level)
        if float(weekly["Close"].iloc[idx - 1]) <= previous_level and float(weekly["Close"].iloc[idx]) > current_level:
            return level
    return None


def _support_hold(weekly, line, level, idx, touch_tolerance_pct):
    support = _line_value(line, idx, level)
    touched = float(weekly["Low"].iloc[idx]) <= support * (1.0 + touch_tolerance_pct)
    held = float(weekly["Close"].iloc[idx]) >= support and float(weekly["Close"].iloc[idx]) > float(weekly["Open"].iloc[idx])
    return touched and held


def entry_risk_reward(weekly, line, level, idx, entry_price):
    """Estimate risk to one ATR below support and reward to the next channel."""
    atr = float(weekly["ATR"].iloc[idx])
    if pd.isna(atr) or atr <= 0:
        return None
    support = _line_value(line, idx, level)
    stop_price = support - atr
    target_price = _line_value(line, idx, level + 1)
    if stop_price <= 0 or entry_price <= stop_price or target_price <= entry_price:
        return None
    risk_pct = (entry_price / stop_price - 1.0) * 100.0
    reward_pct = (target_price / entry_price - 1.0) * 100.0
    return {
        "support_price": support,
        "stop_price": stop_price,
        "target_price": target_price,
        "risk_pct": risk_pct,
        "reward_pct": reward_pct,
        "risk_reward": reward_pct / risk_pct,
    }


def _close_trade(position, df, exit_daily_idx, reason):
    exit_day = df.iloc[exit_daily_idx]
    exit_price = float(exit_day["Open"])
    return {
        "ticker": position["ticker"],
        "entry_date": position["entry_date"],
        "exit_date": df.index[exit_daily_idx].date().isoformat(),
        "entry_price": position["entry_price"],
        "exit_price": exit_price,
        "reason": reason,
        "support_level": position["support_level"],
        "holding_days": exit_daily_idx - position["entry_daily_idx"] + 1,
        "return": (exit_price / position["entry_price"] - 1.0) * 100.0,
    }


def _mark_open_position(position, df):
    mark_idx = len(df) - 1
    mark_price = float(df["Close"].iloc[mark_idx])
    return {
        "ticker": position["ticker"],
        "entry_date": position["entry_date"],
        "mark_date": df.index[mark_idx].date().isoformat(),
        "entry_price": position["entry_price"],
        "mark_price": mark_price,
        "support_level": position["support_level"],
        "holding_days": mark_idx - position["entry_daily_idx"] + 1,
        "return": (mark_price / position["entry_price"] - 1.0) * 100.0,
    }


def run_inbeom_oblique_backtest(
    ticker,
    df=None,
    min_history_weeks=MIN_HISTORY_WEEKS,
    pivot_span_weeks=PIVOT_SPAN_WEEKS,
    min_anchor_separation_weeks=MIN_ANCHOR_SEPARATION_WEEKS,
    min_channel_width_pct=MIN_CHANNEL_WIDTH_PCT,
    retest_window_weeks=RETEST_WINDOW_WEEKS,
    touch_tolerance_pct=TOUCH_TOLERANCE_PCT,
    max_entry_distance_pct=MAX_ENTRY_DISTANCE_PCT,
    max_channel_level=MAX_CHANNEL_LEVEL,
    channel_scale="linear",
    atr_weeks=ATR_WEEKS,
    min_risk_reward=MIN_RISK_REWARD,
):
    """Backtest confirmed high-high parallel-channel breakouts without lookahead."""
    df = _normalize_ohlcv(
        core_market_data_store.get_price_data(ticker, period_days=HISTORY_YEARS * 270) if df is None else df
    )
    weekly = weekly_ohlc(df)
    result = {"closed_trades": [], "open_positions": [], "history_weeks": len(weekly)}
    if weekly.empty or len(weekly) < min_history_weeks:
        return result
    weekly["ATR"] = weekly_atr(weekly, atr_weeks)
    high_pivots = confirmed_pivots(weekly, "High", pivot_span_weeks)
    position = None
    pending_entry = None
    pending_upgrade = None

    for idx in range(1, len(weekly)):
        signal_date = weekly.index[idx]

        if position is not None:
            current_support = _line_value(position["line"], idx, position["support_level"])
            if float(weekly["Close"].iloc[idx]) < current_support:
                exit_daily_idx = _next_daily_idx(df, signal_date)
                if exit_daily_idx is not None:
                    result["closed_trades"].append(
                        _close_trade(position, df, exit_daily_idx, "weekly channel support break")
                    )
                    position = None
                    pending_upgrade = None
                    continue

            next_level = position["support_level"] + 1
            if next_level <= max_channel_level:
                if pending_upgrade and idx > pending_upgrade["expires_idx"]:
                    pending_upgrade = None
                if pending_upgrade is None:
                    crossed = _crossed_above_channel(weekly, position["line"], idx, next_level)
                    if crossed == next_level:
                        pending_upgrade = {"level": next_level, "expires_idx": idx + retest_window_weeks}
                elif _support_hold(weekly, position["line"], next_level, idx, touch_tolerance_pct):
                    position["support_level"] = next_level
                    pending_upgrade = None
            continue

        if pending_entry is not None:
            if idx > pending_entry["expires_idx"]:
                pending_entry = None
            elif _support_hold(
                weekly, pending_entry["line"], pending_entry["level"], idx, touch_tolerance_pct
            ):
                entry_daily_idx = _next_daily_idx(df, signal_date)
                support = _line_value(pending_entry["line"], idx, pending_entry["level"])
                if entry_daily_idx is not None:
                    entry_price = float(df["Open"].iloc[entry_daily_idx])
                    risk_reward = entry_risk_reward(
                        weekly, pending_entry["line"], pending_entry["level"], idx, entry_price
                    )
                    if (
                        support <= entry_price <= support * (1.0 + max_entry_distance_pct)
                        and (
                            min_risk_reward <= 0
                            or (risk_reward is not None and risk_reward["risk_reward"] >= min_risk_reward)
                        )
                    ):
                        position = {
                            "ticker": ticker,
                            "entry_daily_idx": entry_daily_idx,
                            "entry_date": df.index[entry_daily_idx].date().isoformat(),
                            "entry_price": entry_price,
                            "line": pending_entry["line"],
                            "support_level": pending_entry["level"],
                        }
                pending_entry = None
                continue

        line = latest_historical_high_channel(
            high_pivots,
            idx,
            min_anchor_separation_weeks=min_anchor_separation_weeks,
            min_channel_width_pct=min_channel_width_pct,
            channel_scale=channel_scale,
        )
        if line is None:
            continue
        level = _crossed_above_channel(weekly, line, idx, max_channel_level)
        if level is not None:
            pending_entry = {"line": line, "level": level, "expires_idx": idx + retest_window_weeks}

    if position is not None:
        result["open_positions"].append(_mark_open_position(position, df))
    return result


def _stats(trades):
    returns = [trade["return"] for trade in trades]
    holding_days = [trade["holding_days"] for trade in trades]
    if not returns:
        return {"count": 0, "avg": 0.0, "median": 0.0, "win_rate": 0.0, "avg_hold": 0.0}
    return {
        "count": len(returns),
        "avg": sum(returns) / len(returns),
        "median": median(returns),
        "win_rate": sum(value > 0 for value in returns) / len(returns) * 100.0,
        "avg_hold": sum(holding_days) / len(holding_days),
    }


def _print_stats(label, trades):
    stats = _stats(trades)
    print(
        f"{label}: trades {stats['count']} | avg {stats['avg']:+.2f}% | "
        f"median {stats['median']:+.2f}% | win {stats['win_rate']:.1f}% | "
        f"avg hold {stats['avg_hold']:.1f}d"
    )


def _print_open_position(position):
    print(
        f"  OPEN {position['entry_date']} -> {position['mark_date']} | "
        f"channel {position['support_level']} | {position['holding_days']}d | "
        f"mark {position['return']:+.2f}%"
    )


def run_terminal_experiment(tickers, show_trades=False, history_years=HISTORY_YEARS, **kwargs):
    all_closed, all_open, insufficient, no_data = [], [], [], []
    history_days = history_years * 270
    for ticker in sorted(set(tickers)):
        df = _normalize_ohlcv(core_market_data_store.get_price_data(ticker, period_days=history_days))
        if df.empty:
            no_data.append(ticker)
            continue
        result = run_inbeom_oblique_backtest(ticker, df=df, **kwargs)
        if result["history_weeks"] < kwargs.get("min_history_weeks", MIN_HISTORY_WEEKS):
            insufficient.append(ticker)
            continue
        closed_trades = result["closed_trades"]
        open_positions = result["open_positions"]
        all_closed.extend(closed_trades)
        all_open.extend(open_positions)
        if closed_trades:
            _print_stats(ticker, closed_trades)
        elif open_positions:
            print(f"{ticker}: open positions {len(open_positions)}")
        if show_trades:
            for trade in closed_trades:
                print(
                    f"  {trade['entry_date']} -> {trade['exit_date']} | {trade['reason']} | "
                    f"channel {trade['support_level']} | {trade['holding_days']}d | {trade['return']:+.2f}%"
                )
            for position in open_positions:
                _print_open_position(position)
    print()
    _print_stats("ALL CLOSED", all_closed)
    if all_open:
        print(f"OPEN POSITIONS: {len(all_open)} (excluded from closed-trade statistics)")
        if show_trades:
            for position in all_open:
                _print_open_position(position)
    if insufficient:
        print(f"Insufficient {history_years}y weekly history:", ", ".join(insufficient))
    if no_data:
        print("No cached data:", ", ".join(no_data))
    return {"closed_trades": all_closed, "open_positions": all_open}


def _completed_weekly_ohlc(df):
    """Exclude the still-forming current week from a live screen."""
    weekly = weekly_ohlc(df)
    if weekly.empty:
        return weekly
    current_week = pd.Timestamp.now().to_period("W-FRI")
    if weekly.index[-1].to_period("W-FRI") == current_week:
        return weekly.iloc[:-1].copy()
    return weekly


def _screen_snapshot(ticker, weekly, line, level, idx, breakout_idx, status):
    support = _line_value(line, idx, level)
    reference_price = float(weekly["Close"].iloc[idx])
    risk_reward = entry_risk_reward(weekly, line, level, idx, reference_price)
    return {
        "ticker": ticker,
        "status": status,
        "signal_date": weekly.index[idx].date().isoformat(),
        "breakout_date": weekly.index[breakout_idx].date().isoformat(),
        "level": level,
        "last_close": reference_price,
        "support": support,
        "distance_to_support_pct": (reference_price / support - 1.0) * 100.0,
        "estimated_risk_reward": None if risk_reward is None else risk_reward["risk_reward"],
        "base_anchors": [
            {
                "kind": "HIGH",
                "date": weekly.index[line["first"]["pivot_idx"]].date().isoformat(),
                "price": line["first"]["price"],
            },
            {
                "kind": "HIGH",
                "date": weekly.index[line["second"]["pivot_idx"]].date().isoformat(),
                "price": line["second"]["price"],
            },
        ],
        "channel_reference": {
            "kind": "HIGH",
            "date": weekly.index[line["third"]["pivot_idx"]].date().isoformat(),
            "price": line["third"]["price"],
            "level": 1,
        },
        "support_channel": {
            "level": level,
            "price": support,
            "as_of": weekly.index[idx].date().isoformat(),
        },
    }


def screen_inbeom_candidate(
    ticker,
    df,
    min_history_weeks=MIN_HISTORY_WEEKS,
    pivot_span_weeks=PIVOT_SPAN_WEEKS,
    min_anchor_separation_weeks=MIN_ANCHOR_SEPARATION_WEEKS,
    min_channel_width_pct=MIN_CHANNEL_WIDTH_PCT,
    retest_window_weeks=RETEST_WINDOW_WEEKS,
    touch_tolerance_pct=TOUCH_TOLERANCE_PCT,
    max_channel_level=MAX_CHANNEL_LEVEL,
    atr_weeks=ATR_WEEKS,
    channel_scale="linear",
):
    """Return the current completed-week READY/WATCH setup, if one exists."""
    weekly = _completed_weekly_ohlc(_normalize_ohlcv(df))
    if weekly.empty or len(weekly) < min_history_weeks:
        return {"status": "INSUFFICIENT", "history_weeks": len(weekly)}
    weekly["ATR"] = weekly_atr(weekly, atr_weeks)
    high_pivots = confirmed_pivots(weekly, "High", pivot_span_weeks)
    pending_entry = None
    last_idx = len(weekly) - 1

    for idx in range(1, len(weekly)):
        if pending_entry is not None:
            if idx > pending_entry["expires_idx"]:
                pending_entry = None
            elif _support_hold(
                weekly, pending_entry["line"], pending_entry["level"], idx, touch_tolerance_pct
            ):
                if idx == last_idx:
                    return _screen_snapshot(
                        ticker,
                        weekly,
                        pending_entry["line"],
                        pending_entry["level"],
                        idx,
                        pending_entry["breakout_idx"],
                        "READY",
                    )
                pending_entry = None
                continue

        line = latest_historical_high_channel(
            high_pivots,
            idx,
            min_anchor_separation_weeks=min_anchor_separation_weeks,
            min_channel_width_pct=min_channel_width_pct,
            channel_scale=channel_scale,
        )
        if line is None:
            continue
        level = _crossed_above_channel(weekly, line, idx, max_channel_level)
        if level is not None:
            pending_entry = {
                "line": line,
                "level": level,
                "breakout_idx": idx,
                "expires_idx": idx + retest_window_weeks,
            }

    if pending_entry is None:
        return None
    support = _line_value(pending_entry["line"], last_idx, pending_entry["level"])
    if float(weekly["Close"].iloc[last_idx]) < support:
        return None
    return _screen_snapshot(
        ticker,
        weekly,
        pending_entry["line"],
        pending_entry["level"],
        last_idx,
        pending_entry["breakout_idx"],
        "WATCH",
    )


def _print_screen_candidate(candidate):
    risk_reward = candidate["estimated_risk_reward"]
    risk_reward_text = "n/a" if risk_reward is None else f"{risk_reward:.1f}:1"
    first, second = candidate["base_anchors"]
    reference = candidate["channel_reference"]
    print(
        f"{candidate['status']:<5} {candidate['ticker']:<6} | breakout {candidate['breakout_date']} | "
        f"channel {candidate['level']} | close {candidate['last_close']:.2f} | "
        f"support {candidate['support']:.2f} ({candidate['distance_to_support_pct']:+.1f}%) | "
        f"est. R:R {risk_reward_text}"
    )
    print(
        f"  draw base: {first['date']} {first['kind']} {first['price']:.2f} -> "
        f"{second['date']} {second['kind']} {second['price']:.2f}"
    )
    print(
        f"  channel basis: parallel copy through {reference['date']} {reference['kind']} "
        f"{reference['price']:.2f} (L{reference['level']}) | "
        f"current support: L{candidate['support_channel']['level']} "
        f"{candidate['support_channel']['price']:.2f} as of {candidate['support_channel']['as_of']}"
    )


def run_terminal_screener(tickers, history_years=HISTORY_YEARS, **kwargs):
    candidates, insufficient, no_data = [], [], []
    history_days = history_years * 270
    for ticker in sorted(set(tickers)):
        df = core_market_data_store.get_price_data(ticker, period_days=history_days)
        if df is None or df.empty:
            no_data.append(ticker)
            continue
        candidate = screen_inbeom_candidate(ticker, df, **kwargs)
        if candidate and candidate["status"] == "INSUFFICIENT":
            insufficient.append(ticker)
        elif candidate:
            candidates.append(candidate)

    ready = [candidate for candidate in candidates if candidate["status"] == "READY"]
    watch = [candidate for candidate in candidates if candidate["status"] == "WATCH"]
    print("Inbeom weekly-channel screen (completed weekly bars only)")
    print(f"READY next open: {len(ready)} | WATCH pullback: {len(watch)}")
    for candidate in ready + watch:
        _print_screen_candidate(candidate)
    if insufficient:
        print(f"Insufficient {history_years}y weekly history:", ", ".join(insufficient))
    if no_data:
        print("No cached data:", ", ".join(no_data))
    return {"ready": ready, "watch": watch}


def _parse_tickers(args):
    tickers = [ticker.strip().upper() for ticker in args.tickers.split(",") if ticker.strip()]
    if args.group:
        import config

        tickers.extend(config.universe_groups.get(args.group, []))
    if not tickers:
        raise SystemExit("Provide --tickers AAPL,MSFT or --group <config group>.")
    return tickers


def _backfill_history(tickers, history_years):
    def progress(index, total, ticker):
        print(f"[{index}/{total}] downloading {ticker}")

    summary = core_market_data_store.backfill_price_data(
        tickers,
        period=f"{history_years}y",
        progress_callback=progress,
    )
    print(core_market_data_store.format_cache_summary(summary, title=f"{history_years}Y Inbeom Channel Backfill"))
    return summary


def main():
    parser = argparse.ArgumentParser(description="Standalone Inbeom weekly-channel LONG experiment")
    parser.add_argument("--tickers", default="", help="Comma-separated tickers")
    parser.add_argument("--group", default="", help="Optional configured universe group")
    parser.add_argument("--history-years", type=int, default=HISTORY_YEARS)
    parser.add_argument("--min-history-weeks", type=int, default=MIN_HISTORY_WEEKS)
    parser.add_argument("--pivot-span-weeks", type=int, default=PIVOT_SPAN_WEEKS)
    parser.add_argument("--min-anchor-separation-weeks", type=int, default=MIN_ANCHOR_SEPARATION_WEEKS)
    parser.add_argument("--retest-weeks", type=int, default=RETEST_WINDOW_WEEKS)
    parser.add_argument("--touch-tolerance", type=float, default=TOUCH_TOLERANCE_PCT)
    parser.add_argument("--max-entry-distance", type=float, default=MAX_ENTRY_DISTANCE_PCT)
    parser.add_argument("--atr-weeks", type=int, default=ATR_WEEKS)
    parser.add_argument("--channel-scale", choices=CHANNEL_SCALES, default="linear")
    parser.add_argument("--min-risk-reward", type=float, default=MIN_RISK_REWARD)
    parser.add_argument("--backfill-10y", action="store_true", help="Use existing cache backfill for the selected tickers")
    parser.add_argument("--download-only", action="store_true", help="Exit after --backfill-10y completes")
    parser.add_argument("--screen", action="store_true", help="Show current READY/WATCH candidates from completed weekly bars")
    parser.add_argument("--show-trades", action="store_true")
    args = parser.parse_args()
    if args.history_years < 1:
        parser.error("--history-years must be at least 1")
    if args.download_only and not args.backfill_10y:
        parser.error("--download-only requires --backfill-10y")
    tickers = _parse_tickers(args)
    if args.backfill_10y:
        _backfill_history(tickers, args.history_years)
        if args.download_only:
            return
    if args.screen:
        run_terminal_screener(
            tickers,
            history_years=args.history_years,
            min_history_weeks=args.min_history_weeks,
            pivot_span_weeks=args.pivot_span_weeks,
            min_anchor_separation_weeks=args.min_anchor_separation_weeks,
            retest_window_weeks=args.retest_weeks,
            touch_tolerance_pct=args.touch_tolerance,
            atr_weeks=args.atr_weeks,
            channel_scale=args.channel_scale,
        )
        return
    run_terminal_experiment(
        tickers,
        show_trades=args.show_trades,
        history_years=args.history_years,
        min_history_weeks=args.min_history_weeks,
        pivot_span_weeks=args.pivot_span_weeks,
        min_anchor_separation_weeks=args.min_anchor_separation_weeks,
        retest_window_weeks=args.retest_weeks,
        touch_tolerance_pct=args.touch_tolerance,
        max_entry_distance_pct=args.max_entry_distance,
        channel_scale=args.channel_scale,
        atr_weeks=args.atr_weeks,
        min_risk_reward=args.min_risk_reward,
    )


if __name__ == "__main__":
    main()
