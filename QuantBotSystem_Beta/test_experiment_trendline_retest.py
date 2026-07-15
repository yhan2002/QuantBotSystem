"""Standalone daily trendline-breakout/retest experiment.

This module is intentionally not imported by the bot.  It only reads the
local OHLCV cache and prints experiment results to the terminal.
"""
import argparse
from statistics import median

import pandas as pd

import core_market_data_store


PIVOT_SPAN = 3
RETEST_WINDOW_DAYS = 10
TOUCH_TOLERANCE_PCT = 0.003
STOP_BUFFER_PCT = 0.01
TARGET_R = 2.0
MAX_HOLDING_DAYS = 20

# Current-constituent experiment only. Source: CompaniesMarketCap's U.S.
# ranking captured on 2026-07-15. It is intentionally a fixed snapshot and
# therefore is not suitable for a survivorship-bias-free historical study.
US_LARGE_CAP_100_20260715 = (
    "NVDA", "AAPL", "GOOG", "MSFT", "AMZN", "AVGO", "SPCX", "META", "TSLA", "MU",
    "BRK-B", "LLY", "JPM", "WMT", "AMD", "V", "JNJ", "XOM", "INTC", "MA",
    "AMAT", "CSCO", "LRCX", "ABBV", "CAT", "BAC", "COST", "UNH", "GE", "ORCL",
    "CVX", "MS", "KO", "PG", "HD", "GS", "PLTR", "NFLX", "KLAC", "MRK",
    "DELL", "PANW", "GEV", "TXN", "PM", "RTX", "SNDK", "WFC", "AXP", "ANET",
    "C", "CRWD", "IBM", "TMUS", "MRVL", "TMO", "APH", "WDC", "AMGN", "ADI",
    "MCD", "QCOM", "NEE", "PEP", "VZ", "SCHW", "UNP", "BA", "BLK", "WELL",
    "DIS", "TJX", "IBKR", "GLW", "GILD", "DE", "ABT", "BX", "SCCO", "APP",
    "T", "UBER", "DHR", "PFE", "CRM", "COP", "PLD", "BKNG", "CVS", "ISRG",
    "PGR", "SPGI", "COF", "FTNT", "PH", "SBUX", "VRTX", "SYK", "LMT", "MO",
)
UNIVERSE_SNAPSHOTS = {"us-large-cap-100-20260715": US_LARGE_CAP_100_20260715}


def _normalize_ohlcv(df):
    required = ["Open", "High", "Low", "Close", "Volume"]
    if df is None or df.empty or any(column not in df.columns for column in required):
        return pd.DataFrame()
    return df.dropna(subset=required).copy()


def confirmed_pivot_highs(df, pivot_span=PIVOT_SPAN):
    """Return pivot highs only at the later date when they become knowable."""
    pivots = []
    highs = df["High"].reset_index(drop=True)
    for pivot_idx in range(pivot_span, len(highs) - pivot_span):
        window = highs.iloc[pivot_idx - pivot_span:pivot_idx + pivot_span + 1]
        if highs.iloc[pivot_idx] == window.max() and (window == highs.iloc[pivot_idx]).sum() == 1:
            pivots.append(
                {
                    "pivot_idx": pivot_idx,
                    "confirmed_idx": pivot_idx + pivot_span,
                    "price": float(highs.iloc[pivot_idx]),
                }
            )
    return pivots


def _latest_descending_line(pivots, as_of_idx):
    available = [pivot for pivot in pivots if pivot["confirmed_idx"] <= as_of_idx]
    if len(available) < 2:
        return None
    first, second = available[-2:]
    slope = (second["price"] - first["price"]) / (second["pivot_idx"] - first["pivot_idx"])
    if slope >= 0:
        return None
    return {"first": first, "second": second, "slope": slope}


def _line_value(line, idx):
    return line["second"]["price"] + line["slope"] * (idx - line["second"]["pivot_idx"])


def _stop_fill(day_open, stop_level):
    return min(float(day_open), stop_level)


def run_trendline_retest_backtest(
    ticker,
    df=None,
    pivot_span=PIVOT_SPAN,
    retest_window_days=RETEST_WINDOW_DAYS,
    touch_tolerance_pct=TOUCH_TOLERANCE_PCT,
    stop_buffer_pct=STOP_BUFFER_PCT,
    target_r=TARGET_R,
    max_holding_days=MAX_HOLDING_DAYS,
):
    """Backtest a LONG-only descending-resistance breakout/retest setup.

    Pivot highs become available only after ``pivot_span`` later daily bars.
    A breakout is never bought directly: a later touch/reclaim of that same
    trendline is required and entry occurs at the following open.
    """
    df = _normalize_ohlcv(
        core_market_data_store.get_price_data(ticker, period_days=540) if df is None else df
    )
    if pivot_span < 1 or df.empty or len(df) < 2 * pivot_span + 5:
        return []

    pivots = confirmed_pivot_highs(df, pivot_span)
    trades, pending_retest, position = [], None, None

    for idx in range(1, len(df)):
        day = df.iloc[idx]
        date = df.index[idx].date().isoformat()

        if position is not None:
            stop_line = _line_value(position["line"], idx)
            stop_level = stop_line * (1 - stop_buffer_pct)
            held_days = idx - position["entry_idx"] + 1

            # A close below the line is decisive. If target and stop are both
            # observable in one daily bar, use the conservative stop-first rule.
            if float(day["Close"]) <= stop_level:
                exit_idx = min(idx + 1, len(df) - 1)
                exit_day = df.iloc[exit_idx]
                exit_price = _stop_fill(exit_day["Open"], stop_level)
                trades.append(_close_trade(position, df.index[exit_idx], exit_price, "trendline stop"))
                position = None
                continue
            if float(day["High"]) >= position["target_price"]:
                trades.append(_close_trade(position, df.index[idx], position["target_price"], "2R target"))
                position = None
                continue
            if held_days >= max_holding_days:
                trades.append(_close_trade(position, df.index[idx], float(day["Close"]), "time exit"))
                position = None
                continue
            if idx == len(df) - 1:
                trades.append(_close_trade(position, df.index[idx], float(day["Close"]), "end mark"))
                position = None
            continue

        if pending_retest is not None:
            if idx > pending_retest["expires_idx"]:
                pending_retest = None
            else:
                line_value = _line_value(pending_retest["line"], idx)
                touched = float(day["Low"]) <= line_value * (1 + touch_tolerance_pct)
                reclaimed = float(day["Close"]) > line_value and float(day["Close"]) > float(day["Open"])
                if touched and reclaimed and idx + 1 < len(df):
                    setup_line = pending_retest["line"]
                    entry_idx = idx + 1
                    entry_price = float(df["Open"].iloc[entry_idx])
                    entry_line = _line_value(setup_line, entry_idx)
                    stop_level = entry_line * (1 - stop_buffer_pct)
                    risk = entry_price - stop_level
                    pending_retest = None
                    if risk > 0:
                        position = {
                            "ticker": ticker,
                            "entry_idx": entry_idx,
                            "entry_date": df.index[entry_idx].date().isoformat(),
                            "entry_price": entry_price,
                            "line": setup_line,
                            "target_price": entry_price + target_r * risk,
                        }
                    continue

        line = _latest_descending_line(pivots, idx)
        if line is None:
            continue
        previous_line = _line_value(line, idx - 1)
        current_line = _line_value(line, idx)
        if float(df["Close"].iloc[idx - 1]) <= previous_line and float(day["Close"]) > current_line:
            pending_retest = {
                "line": line,
                "expires_idx": idx + retest_window_days,
                "breakout_date": date,
            }

    return trades


def _close_trade(position, exit_timestamp, exit_price, reason):
    trade_return = (exit_price / position["entry_price"] - 1.0) * 100.0
    return {
        "ticker": position["ticker"],
        "entry_date": position["entry_date"],
        "exit_date": exit_timestamp.date().isoformat(),
        "entry_price": position["entry_price"],
        "exit_price": exit_price,
        "reason": reason,
        "return": trade_return,
    }


def _stats(trades):
    values = [trade["return"] for trade in trades]
    if not values:
        return {"count": 0, "avg": 0.0, "median": 0.0, "win_rate": 0.0}
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "median": median(values),
        "win_rate": sum(value > 0 for value in values) / len(values) * 100.0,
    }


def _print_stats(label, trades):
    stats = _stats(trades)
    print(
        f"{label}: trades {stats['count']} | avg {stats['avg']:+.2f}% | "
        f"median {stats['median']:+.2f}% | win {stats['win_rate']:.1f}%"
    )


def run_terminal_experiment(tickers, show_trades=False, **kwargs):
    all_trades, skipped = [], []
    for ticker in sorted(set(tickers)):
        df = _normalize_ohlcv(core_market_data_store.get_price_data(ticker, period_days=540))
        if df.empty:
            skipped.append(ticker)
            continue
        trades = run_trendline_retest_backtest(ticker, df=df, **kwargs)
        all_trades.extend(trades)
        _print_stats(ticker, trades)
        if show_trades:
            for trade in trades:
                print(
                    f"  {trade['entry_date']} -> {trade['exit_date']} | {trade['reason']} | "
                    f"{trade['return']:+.2f}%"
                )
    print()
    _print_stats("ALL", all_trades)
    if skipped:
        print("No cached data:", ", ".join(skipped))
    return all_trades


def _parse_tickers(args):
    tickers = [ticker.strip().upper() for ticker in args.tickers.split(",") if ticker.strip()]
    if args.snapshot:
        tickers.extend(UNIVERSE_SNAPSHOTS[args.snapshot])
    if args.group:
        import config

        tickers.extend(config.universe_groups.get(args.group, []))
    if not tickers:
        raise SystemExit("Provide --tickers AAPL,MSFT, --group <config group>, or --snapshot <name>.")
    return tickers


def main():
    parser = argparse.ArgumentParser(description="Standalone trendline breakout/retest experiment")
    parser.add_argument("--tickers", default="", help="Comma-separated cached tickers, e.g. AAPL,MSFT,NVDA")
    parser.add_argument("--group", default="", help="Optional configured universe group")
    parser.add_argument("--snapshot", choices=sorted(UNIVERSE_SNAPSHOTS), help="Fixed experimental universe snapshot")
    parser.add_argument("--pivot-span", type=int, default=PIVOT_SPAN)
    parser.add_argument("--retest-days", type=int, default=RETEST_WINDOW_DAYS)
    parser.add_argument("--touch-tolerance", type=float, default=TOUCH_TOLERANCE_PCT)
    parser.add_argument("--stop-buffer", type=float, default=STOP_BUFFER_PCT)
    parser.add_argument("--target-r", type=float, default=TARGET_R)
    parser.add_argument("--max-holding-days", type=int, default=MAX_HOLDING_DAYS)
    parser.add_argument("--show-trades", action="store_true")
    args = parser.parse_args()
    run_terminal_experiment(
        _parse_tickers(args),
        show_trades=args.show_trades,
        pivot_span=args.pivot_span,
        retest_window_days=args.retest_days,
        touch_tolerance_pct=args.touch_tolerance,
        stop_buffer_pct=args.stop_buffer,
        target_r=args.target_r,
        max_holding_days=args.max_holding_days,
    )


if __name__ == "__main__":
    main()
