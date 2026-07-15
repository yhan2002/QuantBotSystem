# File name: value_reversion_engine.py
# Price/liquidity reversion: excess move, reversal confirmation, and scale-out exits.
import math

import pandas as pd

import market_data_store


REVERSION_SHOCK_DAYS = 5
REVERSION_VOL_WINDOW = 20
REVERSION_MA_WINDOW = 60
REVERSION_ATR_WINDOW = 14
REVERSION_TURNOVER_LOOKBACK = 20
REVERSION_LONG_THRESHOLD = -1.40
REVERSION_SHORT_THRESHOLD = 1.60
REVERSION_MAX_TURNOVER_RATIO = 2.00
REVERSION_LONG_CLOSE_LOCATION = 0.55
REVERSION_SHORT_CLOSE_LOCATION = 0.45
REVERSION_TREND_SLOPE_TOLERANCE = 0.001
REVERSION_PARTIAL_LEVEL = 0.50
REVERSION_NO_PROGRESS_DAYS = 3
REVERSION_MAX_HOLDING_DAYS = 10
REVERSION_STOP_ATR = 1.50


def _normalize_ohlcv(prices):
    if prices is None or prices.empty:
        return pd.DataFrame()
    if isinstance(prices.columns, pd.MultiIndex):
        prices = prices.copy()
        prices.columns = prices.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in prices.columns for column in required):
        return pd.DataFrame()
    return prices.dropna(subset=required).copy()


def _add_reversion_features(prices):
    prices = _normalize_ohlcv(prices)
    if prices.empty:
        return prices

    close = prices["Close"].astype(float)
    high = prices["High"].astype(float)
    low = prices["Low"].astype(float)
    volume = prices["Volume"].astype(float)
    daily_return = close.pct_change()
    volatility = daily_return.rolling(REVERSION_VOL_WINDOW).std(ddof=0)

    prices["SHOCK"] = close.pct_change(REVERSION_SHOCK_DAYS) / (
        volatility * math.sqrt(REVERSION_SHOCK_DAYS)
    )
    prices["MA60"] = close.rolling(REVERSION_MA_WINDOW).mean()
    prices["MA60_SLOPE"] = prices["MA60"].pct_change(5)
    prices["TURNOVER_RATIO"] = (volume * close) / (
        (volume * close).shift(1).rolling(REVERSION_TURNOVER_LOOKBACK).mean()
    )

    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    prices["ATR"] = true_range.rolling(REVERSION_ATR_WINDOW).mean()
    range_size = (high - low).replace(0, pd.NA)
    prices["CLOSE_LOCATION"] = ((close - low) / range_size).fillna(0.5)

    # Permit a close-confirmed reversal up to three sessions after the shock.
    recent_long_extreme = (
        prices["SHOCK"].shift(1).rolling(3).min() <= REVERSION_LONG_THRESHOLD
    )
    recent_short_extreme = (
        prices["SHOCK"].shift(1).rolling(3).max() >= REVERSION_SHORT_THRESHOLD
    )
    confirmation_long = (close > close.shift(1)) & (
        prices["CLOSE_LOCATION"] >= REVERSION_LONG_CLOSE_LOCATION
    ) & (close >= prices["Open"].astype(float))
    confirmation_short = (close < close.shift(1)) & (
        prices["CLOSE_LOCATION"] <= REVERSION_SHORT_CLOSE_LOCATION
    )
    liquid_enough = prices["TURNOVER_RATIO"] < REVERSION_MAX_TURNOVER_RATIO
    prices["LONG_SIGNAL"] = (
        recent_long_extreme
        & confirmation_long
        & liquid_enough
        & (prices["MA60_SLOPE"] >= -REVERSION_TREND_SLOPE_TOLERANCE)
    ).fillna(False)
    prices["SHORT_SIGNAL"] = (
        recent_short_extreme
        & confirmation_short
        & liquid_enough
        & (prices["MA60_SLOPE"] <= REVERSION_TREND_SLOPE_TOLERANCE)
    ).fillna(False)
    return prices


def analyze_value_reversion(ticker, allow_fundamental_download=True, df=None):
    """Return a confirmed price/liquidity reversion signal known at today's close."""
    del allow_fundamental_download
    prices = (
        market_data_store.get_price_data(ticker, period_days=130)
        if df is None
        else df
    )
    features = _add_reversion_features(prices)
    minimum_rows = REVERSION_MA_WINDOW + REVERSION_SHOCK_DAYS + 2
    if features.empty or len(features) < minimum_rows:
        return {"status": "DATA_INSUFFICIENT", "score": 0.0}

    latest = features.iloc[-1]
    recent_shocks = features["SHOCK"].iloc[-4:-1].dropna()
    previous_shock = float(recent_shocks.iloc[-1]) if not recent_shocks.empty else 0.0
    if bool(latest["LONG_SIGNAL"]):
        return {
            "status": "OK",
            "score": abs(float(recent_shocks.min())),
            "side": "LONG",
            "shock": previous_shock,
            "turnover_ratio": float(latest["TURNOVER_RATIO"]),
            "current_price": float(latest["Close"]),
        }
    if bool(latest["SHORT_SIGNAL"]):
        return {
            "status": "OK",
            "score": -abs(float(recent_shocks.max())),
            "side": "SHORT",
            "shock": previous_shock,
            "turnover_ratio": float(latest["TURNOVER_RATIO"]),
            "current_price": float(latest["Close"]),
        }
    return {"status": "NO_SIGNAL", "score": 0.0}


def _pnl_pct(side, exit_price, entry_price):
    direction = 1.0 if side == "LONG" else -1.0
    return direction * (exit_price - entry_price) / entry_price * 100.0


def _stop_hit(side, day, stop_price):
    return day["Low"] <= stop_price if side == "LONG" else day["High"] >= stop_price


def _stop_fill(side, day_open, stop_price):
    return min(day_open, stop_price) if side == "LONG" else max(day_open, stop_price)


def _post_partial_stop_price(side, entry_price, current_stop):
    """Protect a confirmed partial gain without weakening conservative gap fills."""
    return max(current_stop, entry_price) if side == "LONG" else min(current_stop, entry_price)


def _format_price(value):
    return f"{value:,.2f}" if value < 1000 else f"{value:,.0f}"


def _format_rows(items, long_side=True):
    if not items:
        return "None\n"
    direction = "LONG" if long_side else "SHORT"
    return "\n".join(
        f"{idx}. {row['ticker']:<8} | {direction} | price {_format_price(row['current_price'])} | "
        f"reversion score {row['score']:+.2f} | turnover {row['turnover_ratio']:.1f}x"
        for idx, row in enumerate(items, start=1)
    ) + "\n"


def run_reversion_backtest(ticker, side, period_days=260, include_trades=False, df=None):
    """Backtest confirmed reversion entries and B-style two-stage profit taking.

    All indicator exits are confirmed at the close and filled at the next open.
    ATR stops are intraday protective exits and use a conservative gap fill.
    """
    prices = market_data_store.get_price_data(ticker, period_days=period_days) if df is None else df
    features = _add_reversion_features(prices)
    # MA60 needs five additional observations for its trend slope.
    warmup = REVERSION_MA_WINDOW + 5
    if features.empty or len(features) < warmup + 2:
        result = (0.0, 0.0, 0, "N/A", [])
        return result if include_trades else result[:4]

    period_label = f"{features.index[warmup].date()} to {features.index[-1].date()}"
    trades, trade_details = [], []
    position = None

    def add_exit(current_position, idx, reason, weight, price):
        current_position["realized"] += weight * _pnl_pct(
            current_position["side"], price, current_position["entry_price"]
        )
        current_position["remaining"] -= weight
        current_position["exit_events"].append(
            {
                "date": features.index[idx].date().isoformat(),
                "reason": reason,
                "weight": weight,
                "price": price,
            }
        )

    def finish_trade(current_position):
        trades.append(current_position["realized"])
        trade_details.append(
            {
                "side": current_position["side"],
                "entry_date": current_position["entry_date"],
                "exit_events": current_position["exit_events"],
                "return": current_position["realized"],
            }
        )

    for idx in range(warmup, len(features) - 1):
        day = features.iloc[idx]
        if position is not None and position.get("pending_exit"):
            pending = position.pop("pending_exit")
            fill = float(day["Open"])
            weight = position["remaining"] if pending["final"] else position["remaining"] * 0.5
            add_exit(position, idx, pending["reason"], weight, fill)
            if pending["final"]:
                finish_trade(position)
                position = None
                continue
            position["stop_price"] = _post_partial_stop_price(
                position["side"], position["entry_price"], position["stop_price"]
            )

        if position is None:
            signal_side = "LONG" if bool(day["LONG_SIGNAL"]) else "SHORT" if bool(day["SHORT_SIGNAL"]) else None
            if signal_side is None or side not in (None, signal_side):
                continue
            entry_idx = idx + 1
            entry_price = float(features["Open"].iloc[entry_idx])
            atr = float(day["ATR"])
            if entry_price <= 0 or not pd.notna(atr) or atr <= 0:
                continue
            stop_price = entry_price - REVERSION_STOP_ATR * atr if signal_side == "LONG" else entry_price + REVERSION_STOP_ATR * atr
            position = {
                "side": signal_side,
                "entry_price": entry_price,
                "entry_idx": entry_idx,
                "entry_date": features.index[entry_idx].date().isoformat(),
                "atr": atr,
                "stop_price": stop_price,
                "remaining": 1.0,
                "realized": 0.0,
                "partial_taken": False,
                "best_price": entry_price,
                "exit_events": [],
            }
            continue

        side_name = position["side"]
        if _stop_hit(side_name, day, position["stop_price"]):
            fill = _stop_fill(side_name, float(day["Open"]), position["stop_price"])
            add_exit(position, idx, "ATR stop", position["remaining"], fill)
            finish_trade(position)
            position = None
            continue

        if side_name == "LONG":
            position["best_price"] = max(position["best_price"], float(day["High"]))
            partial_reached = float(day["SHOCK"]) >= -REVERSION_PARTIAL_LEVEL
            mean_reached = float(day["SHOCK"]) >= 0.0
            progressed = position["best_price"] >= position["entry_price"] + 0.5 * position["atr"]
        else:
            position["best_price"] = min(position["best_price"], float(day["Low"]))
            partial_reached = float(day["SHOCK"]) <= REVERSION_PARTIAL_LEVEL
            mean_reached = float(day["SHOCK"]) <= 0.0
            progressed = position["best_price"] <= position["entry_price"] - 0.5 * position["atr"]

        held_days = idx - position["entry_idx"] + 1
        if mean_reached:
            position["pending_exit"] = {"reason": "mean reversion", "final": True}
        elif not position["partial_taken"] and partial_reached:
            position["pending_exit"] = {"reason": "partial at ±0.5σ", "final": False}
            position["partial_taken"] = True
        elif held_days >= REVERSION_NO_PROGRESS_DAYS and not progressed:
            position["pending_exit"] = {"reason": "3d no progress", "final": True}
        elif held_days >= REVERSION_MAX_HOLDING_DAYS:
            position["pending_exit"] = {"reason": "10d time exit", "final": True}

    if not trades:
        result = (0.0, 0.0, 0, period_label, [])
        return result if include_trades else result[:4]
    avg_return = sum(trades) / len(trades)
    win_rate = sum(value > 0 for value in trades) / len(trades) * 100.0
    result = (avg_return, win_rate, len(trades), period_label, trade_details)
    return result if include_trades else result[:4]


def _format_trade_dates(trade):
    events = ", ".join(f"{item['date']} ({item['reason']})" for item in trade["exit_events"])
    return f"{trade['entry_date']} -> {events} {trade['return']:+.1f}%"


def _format_backtest(targets):
    if not targets:
        return ""
    report = "\nBacktest Summary\n-----------------------------------------\n"
    report += (
        "Entry: shock within prior 3d, near-flat/trend-aligned MA60, close-confirmed reversal, "
        "positive LONG candle, and turnover <2.0x at close; enter next open.\n"
        "Exit: 50% at ±0.5σ, move residual stop to entry, then exit at mean (0σ); "
        "ATR stop, 3d no-progress, or 10d time exit.\n"
    )
    total_return_sum = total_wins = 0.0
    total_trades = 0
    for row, side in targets:
        avg_return, win_rate, count, period_label, details = run_reversion_backtest(
            row["ticker"], side, include_trades=True
        )
        total_return_sum += avg_return * count
        total_wins += win_rate / 100.0 * count
        total_trades += count
        if count:
            report += (
                f"{side} *{row['ticker']}*: avg *{avg_return:+.1f}%* | "
                f"win {win_rate:.1f}% ({round(win_rate / 100.0 * count)}/{count}) | "
                f"period {period_label}\n"
                f"  trades: {'; '.join(_format_trade_dates(trade) for trade in details)}\n"
            )
        else:
            report += f"{side} *{row['ticker']}*: no closed trades\n"
    avg = total_return_sum / total_trades if total_trades else 0.0
    win = total_wins / total_trades * 100.0 if total_trades else 0.0
    report += "\nSelected Candidate History Summary\n"
    report += f"*Avg trade return {avg:+.1f}%* | win {win:.1f}% ({round(total_wins)}/{total_trades})\n"
    report += "-----------------------------------------\n"
    return report


def scan_value_reversion(universe_list):
    scored, failed = [], []
    for ticker in universe_list:
        result = analyze_value_reversion(ticker, allow_fundamental_download=False)
        if result["status"] == "OK":
            scored.append({"ticker": ticker, **result})
        elif result["status"] == "DATA_INSUFFICIENT":
            failed.append(ticker)

    top_longs = sorted((row for row in scored if row["side"] == "LONG"), key=lambda row: row["score"], reverse=True)[:3]
    top_shorts = sorted((row for row in scored if row["side"] == "SHORT"), key=lambda row: row["score"])[:3]
    targets = [(row, "LONG") for row in top_longs] + [(row, "SHORT") for row in top_shorts]
    msg = (
        "**Price/Liquidity Reversion v2 Scan Report**\n"
        "=============================\n\n"
        "**LONG Candidates**\n"
        "Rule: 5d downside shock <= -1.40σ within 3d, near-flat/up MA60, positive rebound close, turnover <2.0x.\n"
        f"{_format_rows(top_longs, long_side=True)}\n"
        "**SHORT Candidates**\n"
        "Rule: 5d upside shock >= +1.60σ within 3d, near-flat/down MA60, reversal close, turnover <2.0x.\n"
        f"{_format_rows(top_shorts, long_side=False)}"
        f"{_format_backtest(targets)}"
    )
    if failed:
        msg += "\n----------------------------------------\nInsufficient local data: " + ", ".join(sorted(failed))
    return msg
