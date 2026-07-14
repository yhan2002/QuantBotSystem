# File name: tune_momentum_no_lookahead.py
# Runs no-lookahead walk-forward tuning for the MA momentum strategy.
import math
import os
import sqlite3
from collections import defaultdict

import pandas as pd

import config


DEFAULT_GROUPS = ["CH_UNIV", "JP_UNIV", "CSQ_UNIV"]
MA_WINDOWS = [(5, 10, 20), (5, 20, 60), (10, 20, 60)]
SLOPE_THRESHOLDS = [0.0, 0.2, 0.4, 0.6, 0.8]
HOLDING_DAYS = [5, 10, 20]
TOP_NS = [5, 10]
PORTFOLIO_MODES = ["LONG_ONLY", "LONG_BIASED_70_30"]

MIN_COHORTS = 40
MIN_TRADES = 100
MIN_WIN_RATE = 52.0
MIN_T_VALUE = 2.58
MAX_RESULTS_TO_PRINT = 25

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "market_data.sqlite")


def selected_universe():
    tickers = set()
    for group in DEFAULT_GROUPS:
        tickers.update(config.universe_groups.get(group, []))
    return sorted(tickers)


def load_price_map(tickers):
    if not os.path.exists(DB_PATH):
        return {}

    conn = sqlite3.connect(DB_PATH)
    try:
        placeholders = ",".join("?" for _ in tickers)
        df = pd.read_sql_query(
            f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM ohlcv
            WHERE ticker IN ({placeholders})
            ORDER BY ticker, date
            """,
            conn,
            params=tickers,
        )
    finally:
        conn.close()

    if df.empty:
        return {}

    df["date"] = pd.to_datetime(df["date"])
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )

    price_map = {}
    for ticker, group in df.groupby("ticker", sort=False):
        clean = group.drop(columns=["ticker"]).set_index("date")
        clean = clean.dropna(subset=["Open", "Close"]).copy()
        if not clean.empty:
            price_map[ticker] = clean
    return price_map


def build_signal_frame(df, windows, threshold):
    fast, mid, slow = windows
    out = df[["Open", "Close"]].copy()
    out["MA_FAST"] = out["Close"].rolling(fast).mean()
    out["MA_MID"] = out["Close"].rolling(mid).mean()
    out["MA_SLOW"] = out["Close"].rolling(slow).mean()
    out["S_FAST"] = out["MA_FAST"].pct_change() * 100.0
    out["S_MID"] = out["MA_MID"].pct_change() * 100.0
    out["S_SLOW"] = out["MA_SLOW"].pct_change() * 100.0

    out["LONG_SIGNAL"] = (
        (out["MA_FAST"] > out["MA_MID"])
        & (out["MA_MID"] > out["MA_SLOW"])
        & (out["S_FAST"] > threshold)
        & (out["S_MID"] > threshold)
        & (out["S_SLOW"] > threshold)
    )
    out["SHORT_SIGNAL"] = (
        (out["MA_FAST"] < out["MA_MID"])
        & (out["MA_MID"] < out["MA_SLOW"])
        & (out["S_FAST"] < -threshold)
        & (out["S_MID"] < -threshold)
        & (out["S_SLOW"] < -threshold)
    )
    out["LONG_SCORE"] = (out["S_FAST"] + out["S_MID"] + out["S_SLOW"]) / 3.0
    out["SHORT_SCORE"] = -out["LONG_SCORE"]
    return out


def build_signal_map(price_map, windows, threshold):
    signal_map = {}
    skipped = 0
    for ticker, df in price_map.items():
        if len(df) < max(windows) + max(HOLDING_DAYS) + 2:
            skipped += 1
            continue
        signal_map[ticker] = build_signal_frame(df, windows, threshold)
    return signal_map, skipped


def build_events(signal_map, windows, holding_days):
    events_by_date = defaultdict(list)

    for ticker, signals in signal_map.items():
        if len(signals) < max(windows) + holding_days + 2:
            continue

        dates = signals.index
        opens = signals["Open"].to_numpy()
        closes = signals["Close"].to_numpy()
        long_signal = signals["LONG_SIGNAL"].to_numpy()
        short_signal = signals["SHORT_SIGNAL"].to_numpy()
        long_score = signals["LONG_SCORE"].to_numpy()
        short_score = signals["SHORT_SCORE"].to_numpy()

        for idx in range(max(windows), len(signals) - holding_days):
            entry_idx = idx + 1
            exit_idx = idx + holding_days
            entry = float(opens[entry_idx])
            exit_price = float(closes[exit_idx])
            if entry <= 0:
                continue

            signal_date = dates[idx]
            if bool(long_signal[idx]):
                events_by_date[signal_date].append(
                    {
                        "ticker": ticker,
                        "side": "LONG",
                        "score": float(long_score[idx]),
                        "entry_idx": entry_idx,
                        "exit_idx": exit_idx,
                        "return": (exit_price / entry - 1.0) * 100.0,
                    }
                )
            if bool(short_signal[idx]):
                events_by_date[signal_date].append(
                    {
                        "ticker": ticker,
                        "side": "SHORT",
                        "score": float(short_score[idx]),
                        "entry_idx": entry_idx,
                        "exit_idx": exit_idx,
                        "return": (entry - exit_price) / entry * 100.0,
                    }
                )

    prepared = {}
    for date, events in events_by_date.items():
        prepared[date] = {
            "LONG": sorted(
                [event for event in events if event["side"] == "LONG"],
                key=lambda event: event["score"],
                reverse=True,
            ),
            "SHORT": sorted(
                [event for event in events if event["side"] == "SHORT"],
                key=lambda event: event["score"],
                reverse=True,
            ),
        }
    return prepared


def select_events(events, top_n, mode, active_until):
    longs = events["LONG"]
    shorts = events["SHORT"]

    def available(rows, limit):
        chosen = []
        for event in rows:
            ticker = event["ticker"]
            if event["entry_idx"] <= active_until.get(ticker, -1):
                continue
            chosen.append(event)
            if len(chosen) >= limit:
                break
        return chosen

    if mode == "LONG_ONLY":
        selected_longs = available(longs, top_n)
        return [(event, 1.0 / len(selected_longs)) for event in selected_longs] if selected_longs else []

    if mode == "LONG_BIASED_70_30":
        selected_longs = available(longs, top_n)
        selected_shorts = available(shorts, max(1, math.ceil(top_n / 2)))
        weighted = []
        if selected_longs:
            weighted.extend((event, 0.70 / len(selected_longs)) for event in selected_longs)
        if selected_shorts:
            weighted.extend((event, 0.30 / len(selected_shorts)) for event in selected_shorts)
        if selected_longs and not selected_shorts:
            return [(event, 1.0 / len(selected_longs)) for event in selected_longs]
        return weighted

    if mode == "LONG_SHORT_50_50":
        selected_longs = available(longs, top_n)
        selected_shorts = available(shorts, top_n)
        if not selected_longs or not selected_shorts:
            return []
        return (
            [(event, 0.50 / len(selected_longs)) for event in selected_longs]
            + [(event, 0.50 / len(selected_shorts)) for event in selected_shorts]
        )

    raise ValueError(f"Unknown mode: {mode}")


def summarize_returns(cohort_returns, trade_returns, trade_count):
    if not cohort_returns:
        return None

    avg = sum(cohort_returns) / len(cohort_returns)
    variance = sum((value - avg) ** 2 for value in cohort_returns) / len(cohort_returns)
    std = math.sqrt(variance)
    t_value = avg / std * math.sqrt(len(cohort_returns)) if std > 0 else 0.0
    win_rate = sum(1 for value in cohort_returns if value > 0) / len(cohort_returns) * 100.0
    trade_win = sum(1 for value in trade_returns if value > 0) / len(trade_returns) * 100.0

    return {
        "cohorts": len(cohort_returns),
        "trades": trade_count,
        "avg": avg,
        "std": std,
        "t": t_value,
        "win": win_rate,
        "trade_win": trade_win,
    }


def simulate(events_by_date, top_n, mode):
    active_until = {}
    cohort_returns = []
    trade_returns = []
    trade_count = 0

    for date in sorted(events_by_date):
        selected = select_events(events_by_date[date], top_n, mode, active_until)
        if not selected:
            continue

        cohort_return = 0.0
        for event, weight in selected:
            cohort_return += event["return"] * weight
            trade_returns.append(event["return"])
            trade_count += 1
            active_until[event["ticker"]] = event["exit_idx"]
        cohort_returns.append(cohort_return)

    return summarize_returns(cohort_returns, trade_returns, trade_count)


def result_line(row):
    return (
        f"mode={row['mode']:<17} | MA={row['windows']} | slope>{row['threshold']:.1f}% "
        f"| hold={row['holding']}d | top={row['top_n']} | "
        f"cohorts={row['cohorts']} | trades={row['trades']} | "
        f"avg={row['avg']:+.2f}% | std={row['std']:.2f}% | "
        f"t={row['t']:.2f} | win={row['win']:.1f}% | trade_win={row['trade_win']:.1f}%"
    )


def main():
    print("No-Lookahead MA Momentum Tuner", flush=True)
    print("=" * 72, flush=True)
    print("Rule: signal uses today's close; trade uses next open.", flush=True)
    print("Pure price-and-volume momentum tuner; no external overlay is applied.", flush=True)
    print(f"DB: {DB_PATH}", flush=True)

    universe = selected_universe()
    price_map = load_price_map(universe)

    missing = len(universe) - len(price_map)
    print(
        f"Universe tickers: {len(universe)} | with local data: {len(price_map)} | missing: {missing}",
        flush=True,
    )
    print("-" * 72, flush=True)

    results = []
    for windows in MA_WINDOWS:
        for threshold in SLOPE_THRESHOLDS:
            signal_map, skipped = build_signal_map(price_map, windows, threshold)
            if not signal_map:
                continue
            for holding in HOLDING_DAYS:
                events_by_date = build_events(signal_map, windows, holding)
                if not events_by_date:
                    continue
                for top_n in TOP_NS:
                    for mode in PORTFOLIO_MODES:
                        summary = simulate(events_by_date, top_n, mode)
                        if not summary:
                            continue
                        row = {
                            "mode": mode,
                            "windows": f"{windows[0]}/{windows[1]}/{windows[2]}",
                            "threshold": threshold,
                            "holding": holding,
                            "top_n": top_n,
                            "skipped": skipped,
                            **summary,
                        }
                        results.append(row)

    if not results:
        print("No valid results. Run Admin -> Refresh Selected Universe first.")
        return

    stable = [
        row
        for row in results
        if row["cohorts"] >= MIN_COHORTS
        and row["trades"] >= MIN_TRADES
        and row["win"] >= MIN_WIN_RATE
        and row["t"] >= MIN_T_VALUE
    ]
    stable.sort(key=lambda row: (row["t"], row["avg"], row["win"]), reverse=True)
    results.sort(key=lambda row: (row["t"], row["avg"], row["win"]), reverse=True)

    print("Stable candidates")
    print("-" * 72)
    if stable:
        for row in stable[:MAX_RESULTS_TO_PRINT]:
            print(result_line(row))
    else:
        print(
            "None met all filters "
            f"(t>={MIN_T_VALUE}, win>={MIN_WIN_RATE:.1f}%, trades>={MIN_TRADES})."
        )

    print("\nTop results by t-value")
    print("-" * 72)
    for row in results[:MAX_RESULTS_TO_PRINT]:
        print(result_line(row))

    best = stable[0] if stable else results[0]
    print("\nSuggested first candidate")
    print("-" * 72)
    print(result_line(best))


if __name__ == "__main__":
    main()
