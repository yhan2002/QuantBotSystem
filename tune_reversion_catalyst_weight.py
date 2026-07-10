# File name: tune_reversion_catalyst_weight.py
# Tunes the catalyst overlay weight for the value reversion strategy.
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

import config
import scalping_catalyst_engine as catalyst
import value_reversion_engine


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "market_data.sqlite")
ZSCORE_WINDOW = 5
HOLDING_DAYS_LIST = [1, 3, 5, 10, 20]
TOP_N = 5
MIN_NAMES_PER_DAY = 25
WEIGHTS = [0.00, 0.10, 0.20, 0.30, 0.40, 0.50]
REFRESH_MISSING_FUNDAMENTALS = True
FUNDAMENTAL_FETCH_SLEEP_SECONDS = 0.35


class TeeOutput:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def _resolve_workbook_path():
    path = getattr(config, "SCALPING_WORKBOOK_PATH", "")
    candidates = [path] if os.path.isabs(path) else [
        os.path.abspath(os.path.join(BASE_DIR, path)),
        os.path.abspath(os.path.join(BASE_DIR, "../스캘핑 ver 2.0 - 복사본.xlsx")),
        os.path.abspath(os.path.join(BASE_DIR, "../scalping_sandbox.xlsx")),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return ""


def _load_fundamentals():
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT ticker, shares_outstanding, total_equity
            FROM fundamentals
            WHERE shares_outstanding > 0 AND total_equity > 0
            """
        ).fetchall()
    finally:
        conn.close()
    return {
        ticker: (float(shares), float(equity))
        for ticker, shares, equity in rows
        if shares and equity
    }


def _load_or_fetch_fundamentals(universe):
    fundamentals = _load_fundamentals()
    missing = [ticker for ticker in universe if ticker not in fundamentals]

    if not REFRESH_MISSING_FUNDAMENTALS or not missing:
        return fundamentals, missing, []

    print("Refreshing missing fundamentals")
    print("----------------------------------------")
    print(f"Missing before refresh: {len(missing)} tickers")

    failed = []
    for idx, ticker in enumerate(missing, start=1):
        try:
            data = value_reversion_engine.get_fundamentals(
                ticker,
                allow_download=True,
            )
            if data:
                fundamentals[ticker] = (
                    float(data["shares_outstanding"]),
                    float(data["total_equity"]),
                )
            else:
                failed.append(ticker)
        except Exception:
            failed.append(ticker)

        if idx % 25 == 0 or idx == len(missing):
            print(f"Progress: {idx}/{len(missing)} | still missing {len(failed)}")

        if FUNDAMENTAL_FETCH_SLEEP_SECONDS and idx < len(missing):
            time.sleep(FUNDAMENTAL_FETCH_SLEEP_SECONDS)

    still_missing = [ticker for ticker in universe if ticker not in fundamentals]
    print(f"Missing after refresh: {len(still_missing)} tickers")
    if still_missing:
        print(f"Missing sample: {', '.join(still_missing[:20])}")
    print("")
    return fundamentals, still_missing, failed


def _load_prices(universe):
    universe = sorted(set(universe))
    placeholders = ",".join("?" for _ in universe)
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, date, open, close
            FROM ohlcv
            WHERE ticker IN ({placeholders})
            ORDER BY ticker, date
            """,
            universe,
        ).fetchall()
    finally:
        conn.close()

    prices = defaultdict(list)
    for ticker, date_value, open_value, close_value in rows:
        if open_value is None or close_value is None:
            continue
        prices[ticker].append(
            {
                "date": str(date_value)[:10],
                "open": float(open_value),
                "close": float(close_value),
            }
        )
    return dict(prices)


def _std(values):
    if len(values) < 2:
        return 0.0
    avg = sum(values) / len(values)
    var = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(var)


def _zscore_latest(values):
    if len(values) < ZSCORE_WINDOW:
        return None
    window = values[-ZSCORE_WINDOW:]
    avg = sum(window) / len(window)
    std = math.sqrt(sum((value - avg) ** 2 for value in window) / len(window))
    if std == 0:
        return None
    return (window[-1] - avg) / std


def _score_series(ticker, rows, fundamentals, holding_days):
    shares, equity = fundamentals[ticker]
    ratios = []
    scored = []
    for idx, row in enumerate(rows):
        ratio = row["close"] * shares / equity
        ratios.append(ratio)
        score = _zscore_latest(ratios)
        if score is None:
            continue
        if idx + holding_days >= len(rows):
            continue
        entry = rows[idx + 1]["open"] if idx + 1 < len(rows) else 0.0
        exit_price = rows[idx + holding_days]["close"]
        if entry <= 0:
            continue
        scored.append(
            {
                "ticker": ticker,
                "date": row["date"],
                "score": -score,
                "return_pct": (exit_price / entry - 1.0) * 100.0,
            }
        )
    return scored


def _latest_catalyst_by_date(workbook_path, universe):
    if not workbook_path:
        return {}
    universe_set = set(universe)
    by_ticker = defaultdict(list)
    for record in catalyst.load_scalping_events(workbook_path):
        ticker = record.get("ticker")
        if ticker not in universe_set or not record.get("event_date"):
            continue
        by_ticker[ticker].append(
            (
                record["event_date"],
                catalyst.fixed_catalyst_score(record),
            )
        )
    for ticker in by_ticker:
        by_ticker[ticker].sort(key=lambda item: item[0])
    return dict(by_ticker)


def _catalyst_score_on(catalyst_history, ticker, date_value):
    rows = catalyst_history.get(ticker, [])
    latest = 0.0
    for event_date, score in rows:
        if event_date > date_value:
            break
        latest = score if score >= catalyst.FIXED_THRESHOLD else 0.0
    return latest


def _summary(returns):
    if not returns:
        return {"count": 0, "avg": 0.0, "std": 0.0, "t": 0.0, "win": 0.0}
    avg = sum(returns) / len(returns)
    std = _std(returns)
    t_value = avg / (std / math.sqrt(len(returns))) if std > 0 else 0.0
    win = sum(1 for value in returns if value > 0) / len(returns) * 100.0
    return {"count": len(returns), "avg": avg, "std": std, "t": t_value, "win": win}


def _rank_rows(rows, strategy_weight, cat_weight):
    max_abs = max(abs(row["score"]) for row in rows) or 1.0
    ranked = []
    for row in rows:
        strategy_norm = abs(row["score"]) / max_abs * 100.0
        combined = strategy_norm * strategy_weight + row["catalyst_score"] * cat_weight
        ranked.append((combined, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _run_daily_top_n(by_date, holding_days, cat_weight, avoid_overlapping_ticker=False):
    strategy_weight = 1.0 - cat_weight
    trades = []
    active_days = 0
    catalyst_selected = 0
    skipped_overlap = 0
    next_available_idx = {}
    date_keys = sorted(by_date)

    for date_idx, date_value in enumerate(date_keys):
        rows = by_date[date_value]
        if len(rows) < MIN_NAMES_PER_DAY:
            continue

        picks = []
        for _, row in _rank_rows(rows, strategy_weight, cat_weight):
            ticker = row["ticker"]
            if avoid_overlapping_ticker and date_idx < next_available_idx.get(ticker, 0):
                skipped_overlap += 1
                continue
            picks.append(row)
            if avoid_overlapping_ticker:
                next_available_idx[ticker] = date_idx + holding_days
            if len(picks) >= TOP_N:
                break

        if not picks:
            continue
        active_days += 1
        catalyst_selected += sum(1 for row in picks if row["catalyst_score"] > 0)
        trades.extend(row["return_pct"] for row in picks)

    summary = _summary(trades)
    summary["active_days"] = active_days
    summary["catalyst_selected"] = catalyst_selected
    summary["skipped_overlap"] = skipped_overlap
    return summary


def run_tuning():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Market DB not found: {DB_PATH}")

    universe = sorted(set(config.universe))
    fundamentals, still_missing_fundamentals, failed_fundamentals = (
        _load_or_fetch_fundamentals(universe)
    )
    prices = _load_prices(universe)
    workbook_path = _resolve_workbook_path()
    catalyst_history = _latest_catalyst_by_date(workbook_path, universe)

    by_date = defaultdict(list)
    skipped_fundamentals = 0
    print("Value Reversion Catalyst Weight Tuning")
    print("Method: daily top-5 long candidates, forward return by holding period")
    print(f"Universe: {len(universe)} tickers")
    print(f"Fundamental coverage: {len(fundamentals)} / {len(universe)} tickers")
    print(f"Skipped fundamentals after refresh: {len(still_missing_fundamentals)} tickers")
    print(f"Workbook: {workbook_path or 'not found'}")
    print("")

    all_results = []
    for holding_days in HOLDING_DAYS_LIST:
        by_date = defaultdict(list)
        skipped_fundamentals = 0
        for ticker in universe:
            if ticker not in fundamentals:
                skipped_fundamentals += 1
                continue
            rows = prices.get(ticker, [])
            if len(rows) < ZSCORE_WINDOW + holding_days + 1:
                continue
            for item in _score_series(ticker, rows, fundamentals, holding_days):
                item["catalyst_score"] = _catalyst_score_on(
                    catalyst_history,
                    ticker,
                    item["date"],
                )
                by_date[item["date"]].append(item)

        print(f"Holding period: {holding_days}d")
        print(f"Skipped fundamentals: {skipped_fundamentals} tickers")
        print("----------------------------------------")

        results = []
        for cat_weight in WEIGHTS:
            summary = _run_daily_top_n(
                by_date,
                holding_days,
                cat_weight,
                avoid_overlapping_ticker=False,
            )
            deoverlap = _run_daily_top_n(
                by_date,
                holding_days,
                cat_weight,
                avoid_overlapping_ticker=True,
            )
            summary["cat_weight"] = cat_weight
            summary["holding_days"] = holding_days
            summary["deoverlap"] = deoverlap
            results.append(summary)
            all_results.append(summary)

            print(
                f"strategy:catalyst={int((1-cat_weight)*100)}%:{int(cat_weight*100)}% | "
                f"count={summary['count']} | avg={summary['avg']:+.2f}% | "
                f"std={summary['std']:.2f}% | t={summary['t']:.2f} | "
                f"win={summary['win']:.1f}% | catalyst picks={summary['catalyst_selected']} | "
                f"no-repeat count={deoverlap['count']} avg={deoverlap['avg']:+.2f}% "
                f"t={deoverlap['t']:.2f} win={deoverlap['win']:.1f}%"
            )
        print("")

    best = sorted(
        all_results,
        key=lambda row: (row["t"], row["avg"], row["win"], row["count"]),
        reverse=True,
    )[0]
    print("")
    print("Best by t-value")
    print("----------------------------------------")
    print(
        f"holding={best['holding_days']}d | "
        f"strategy:catalyst={int((1-best['cat_weight'])*100)}%:{int(best['cat_weight']*100)}% | "
        f"count={best['count']} | avg={best['avg']:+.2f}% | "
        f"std={best['std']:.2f}% | t={best['t']:.2f} | win={best['win']:.1f}%"
    )
    print("")
    print("Caution")
    print("----------------------------------------")
    print("This is a relative tuning test, not a clean point-in-time fundamental backtest.")
    print("no-repeat blocks the same ticker from being re-selected during its holding window.")


if __name__ == "__main__":
    output_path = os.path.join(
        BASE_DIR,
        f"reversion_catalyst_tuning_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    with open(output_path, "w", encoding="utf-8") as output_file:
        original_stdout = sys.stdout
        sys.stdout = TeeOutput(original_stdout, output_file)
        try:
            run_tuning()
            print("")
            print(f"Saved output: {output_path}")
        finally:
            sys.stdout = original_stdout
