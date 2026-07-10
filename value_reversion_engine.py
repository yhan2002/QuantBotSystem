# File name: value_reversion_engine.py
# Ranks cap-to-equity mean reversion signals using cached fundamentals.
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

import catalyst_factor
import market_data_store


FUNDAMENTAL_TTL_DAYS = 30
ZSCORE_WINDOW = 5
REVERSION_STRATEGY_WEIGHT = 0.50
REVERSION_CATALYST_WEIGHT = 0.50
REVERSION_HOLDING_DAYS = 20
REPORT_SHOW_RESEARCH_FIELDS = False


def _get_connection():
    os.makedirs(market_data_store.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(market_data_store.DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker TEXT PRIMARY KEY,
            shares_outstanding REAL,
            total_equity REAL,
            market_cap REAL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def _is_fresh(updated_at):
    try:
        timestamp = datetime.fromisoformat(updated_at)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - timestamp <= timedelta(days=FUNDAMENTAL_TTL_DAYS)
    except Exception:
        return False


def _read_cached_fundamentals(conn, ticker):
    row = conn.execute(
        """
        SELECT shares_outstanding, total_equity, market_cap, updated_at
        FROM fundamentals
        WHERE ticker = ?
        """,
        (ticker,),
    ).fetchone()
    if not row:
        return None
    shares, equity, market_cap, updated_at = row
    if not _is_fresh(updated_at):
        return None
    if not shares or not equity or shares <= 0 or equity <= 0:
        return None
    return {
        "shares_outstanding": float(shares),
        "total_equity": float(equity),
        "market_cap": float(market_cap) if market_cap else None,
        "updated_at": updated_at,
    }


def _first_numeric(series):
    if series is None:
        return None
    for value in series:
        if pd.notna(value):
            try:
                value = float(value)
                if value > 0:
                    return value
            except Exception:
                continue
    return None


def _fetch_fundamentals(ticker):
    yf_ticker = yf.Ticker(ticker)
    info = {}
    try:
        info = yf_ticker.get_info()
    except Exception:
        info = {}

    market_cap = info.get("marketCap")
    shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")

    equity_labels = [
        "Stockholders Equity",
        "Total Stockholder Equity",
        "Common Stock Equity",
        "Total Equity Gross Minority Interest",
        "Total Equity",
        "Shareholders Equity",
    ]

    total_equity = None
    for statement_name in ["balance_sheet", "quarterly_balance_sheet"]:
        try:
            balance_sheet = getattr(yf_ticker, statement_name)
            for label in equity_labels:
                if label in balance_sheet.index:
                    total_equity = _first_numeric(balance_sheet.loc[label])
                    if total_equity:
                        break
            if total_equity:
                break
        except Exception:
            continue

    if (not shares or shares <= 0) and market_cap:
        prices = market_data_store.get_price_data(ticker, period_days=5)
        if not prices.empty:
            latest_close = float(prices["Close"].iloc[-1])
            if latest_close > 0:
                shares = float(market_cap) / latest_close

    book_value = info.get("bookValue")
    if (not total_equity or total_equity <= 0) and shares and book_value:
        try:
            total_equity = float(shares) * float(book_value)
        except Exception:
            total_equity = None

    price_to_book = info.get("priceToBook")
    if (not total_equity or total_equity <= 0) and shares and price_to_book:
        prices = market_data_store.get_price_data(ticker, period_days=5)
        if not prices.empty:
            latest_close = float(prices["Close"].iloc[-1])
            if latest_close > 0 and float(price_to_book) > 0:
                total_equity = float(shares) * latest_close / float(price_to_book)

    if not shares or not total_equity or shares <= 0 or total_equity <= 0:
        return None

    return {
        "shares_outstanding": float(shares),
        "total_equity": float(total_equity),
        "market_cap": float(market_cap) if market_cap else None,
    }


def _upsert_fundamentals(conn, ticker, data):
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO fundamentals (
            ticker, shares_outstanding, total_equity, market_cap, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            shares_outstanding=excluded.shares_outstanding,
            total_equity=excluded.total_equity,
            market_cap=excluded.market_cap,
            updated_at=excluded.updated_at
        """,
        (
            ticker,
            data["shares_outstanding"],
            data["total_equity"],
            data.get("market_cap"),
            updated_at,
        ),
    )
    conn.commit()
    data["updated_at"] = updated_at


def get_fundamentals(ticker, allow_download=True):
    conn = _get_connection()
    try:
        cached = _read_cached_fundamentals(conn, ticker)
        if cached:
            return cached
        if not allow_download:
            return None
        fetched = _fetch_fundamentals(ticker)
        if not fetched:
            return None
        _upsert_fundamentals(conn, ticker, fetched)
        return fetched
    finally:
        conn.close()


def _zscore_latest(series, window=ZSCORE_WINDOW):
    values = series.dropna().tail(window)
    if len(values) < window:
        return None
    std = float(values.std(ddof=0))
    if std == 0 or math.isnan(std):
        return None
    return (float(values.iloc[-1]) - float(values.mean())) / std


def _reversion_signal_series(prices):
    # Avoid injecting today's fundamentals into historical signal dates.
    # Over a short window, zscore(price * constant) is equivalent to zscore(price).
    return prices["Close"].astype(float)


def analyze_value_reversion(ticker, allow_fundamental_download=True):
    prices = market_data_store.get_price_data(ticker, period_days=40)
    if prices.empty or len(prices) < ZSCORE_WINDOW:
        return {"status": "DATA_INSUFFICIENT", "score": 0.0}

    fundamentals = get_fundamentals(ticker, allow_download=allow_fundamental_download)
    if not fundamentals:
        return {"status": "FUNDAMENTAL_MISSING", "score": 0.0}

    signal_series = _reversion_signal_series(prices)
    zscore = _zscore_latest(signal_series)
    if zscore is None:
        return {"status": "NO_SIGNAL", "score": 0.0}

    score = -zscore
    ratio = prices["Close"].astype(float) * fundamentals["shares_outstanding"]
    ratio = ratio / fundamentals["total_equity"]
    latest_ratio = float(ratio.iloc[-1])
    if latest_ratio <= 0.05 or latest_ratio >= 500:
        return {"status": "DATA_SUSPECT", "score": 0.0}

    return {
        "status": "OK",
        "score": score,
        "cap_to_equity": latest_ratio,
        "current_price": float(prices["Close"].iloc[-1]),
        "updated_at": fundamentals.get("updated_at"),
    }


def _format_price(value):
    if value is None:
        return "N/A"
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _enrich_with_catalyst(items, catalyst_map, side):
    if not items:
        return []
    max_score = max(abs(row["score"]) for row in items) or 1.0
    enriched = []
    for row in items:
        strategy_norm = abs(row["score"]) / max_score * 100.0
        cat_score = catalyst_factor.confirmed_score(catalyst_map, row["ticker"])
        if side == "SHORT":
            combined = (
                strategy_norm * REVERSION_STRATEGY_WEIGHT
                - cat_score * REVERSION_CATALYST_WEIGHT
            )
        else:
            combined = (
                strategy_norm * REVERSION_STRATEGY_WEIGHT
                + cat_score * REVERSION_CATALYST_WEIGHT
            )
        copied = dict(row)
        copied["strategy_norm"] = strategy_norm
        copied["catalyst_score"] = cat_score
        copied["combined_score"] = combined
        enriched.append(copied)
    enriched.sort(key=lambda row: row["combined_score"], reverse=True)
    return enriched


def _format_rows(items, long_side=True):
    if not items:
        return "None\n"
    rows = []
    for idx, row in enumerate(items, start=1):
        direction = "LONG" if long_side else "SHORT"
        row_text = (
            f"{idx}. {row['ticker']:<8} | {direction} | "
            f"price {_format_price(row.get('current_price'))} | "
            f"value score {row['score']:+.2f}"
        )
        if REPORT_SHOW_RESEARCH_FIELDS:
            catalyst_text = (
                f" | catalyst {row['catalyst_score']:.1f}"
                if row.get("catalyst_score", 0.0) > 0
                else ""
            )
            row_text += (
                f" | combined {row['combined_score']:.1f}"
                f"{catalyst_text} | cap/equity {row['cap_to_equity']:.2f}"
            )
        rows.append(row_text)
    return "\n".join(rows) + "\n"


def _build_reversion_history(ticker):
    prices = market_data_store.get_price_data(ticker, period_days=260)
    if prices.empty or len(prices) < ZSCORE_WINDOW + REVERSION_HOLDING_DAYS + 1:
        return []

    signal_series = _reversion_signal_series(prices)
    history = []
    for idx in range(ZSCORE_WINDOW - 1, len(prices) - REVERSION_HOLDING_DAYS):
        window = signal_series.iloc[: idx + 1]
        zscore = _zscore_latest(window)
        if zscore is None:
            continue
        score = -zscore
        entry_idx = idx + 1
        exit_idx = idx + REVERSION_HOLDING_DAYS
        entry = float(prices["Open"].iloc[entry_idx])
        exit_price = float(prices["Close"].iloc[exit_idx])
        if entry <= 0:
            continue
        history.append(
            {
                "date": prices.index[idx].date().isoformat(),
                "exit_date": prices.index[exit_idx].date().isoformat(),
                "entry_idx": entry_idx,
                "exit_idx": exit_idx,
                "score": score,
                "long_return": (exit_price / entry - 1.0) * 100.0,
                "short_return": (entry - exit_price) / entry * 100.0,
            }
        )
    return history


def run_reversion_backtest(ticker, side):
    history = _build_reversion_history(ticker)
    if not history:
        return 0.0, 0.0, 0, "N/A"

    trades = []
    next_available_idx = 0
    for row in history:
        if row["entry_idx"] < next_available_idx:
            continue
        if side == "LONG" and row["score"] > 0:
            trades.append(row["long_return"])
            next_available_idx = row["exit_idx"] + 1
        elif side == "SHORT" and row["score"] < 0:
            trades.append(row["short_return"])
            next_available_idx = row["exit_idx"] + 1

    period_label = f"{history[0]['date']} to {history[-1]['exit_date']}"
    if not trades:
        return 0.0, 0.0, 0, period_label

    avg_return = sum(trades) / len(trades)
    win_rate = sum(1 for value in trades if value > 0) / len(trades) * 100.0
    return avg_return, win_rate, len(trades), period_label


def _format_backtest(targets):
    if not targets:
        return ""

    report = "\nBacktest Summary\n"
    report += "-----------------------------------------\n"
    report += (
        f"Rule: no-lookahead value reversion signal, signal close -> next open, "
        f"{REVERSION_HOLDING_DAYS} trading-day holding, non-overlapping entries\n"
        "Guard: historical entries use price z-score proxy only; latest fundamentals "
        "and catalyst are excluded from historical entry logic.\n"
    )

    total_return_sum = 0.0
    total_wins = 0.0
    total_trades = 0
    for row, side in targets:
        avg_return, win_rate, count, period_label = run_reversion_backtest(row["ticker"], side)
        total_return_sum += avg_return * count
        total_wins += (win_rate / 100.0) * count
        total_trades += count
        period_text = f" | period {period_label}" if period_label != "N/A" else ""
        report += (
            f"{side} *{row['ticker']}*: avg *{avg_return:+.1f}%* | "
            f"win {win_rate:.1f}% | {count} trades{period_text}\n"
        )

    total_win_rate = total_wins / total_trades * 100.0 if total_trades else 0.0
    portfolio_avg = total_return_sum / total_trades if total_trades else 0.0
    report += "\nSelected Candidate History Summary\n"
    report += (
        f"*Avg trade return {portfolio_avg:+.1f}%* | "
        f"win {total_win_rate:.1f}% | {total_trades} trades\n"
    )
    report += "-----------------------------------------\n"
    return report


def scan_value_reversion(universe_list):
    scored = []
    failed = []

    for ticker in universe_list:
        result = analyze_value_reversion(ticker)
        if result["status"] != "OK":
            failed.append(f"{ticker}({result['status']})")
            continue
        scored.append(
            {
                "ticker": ticker,
                "score": result["score"],
                "cap_to_equity": result["cap_to_equity"],
                "current_price": result["current_price"],
            }
        )

    catalyst_map = catalyst_factor.latest_catalyst_map(universe_list)

    long_pool = [row for row in scored if row["score"] > 0]
    short_pool = [row for row in scored if row["score"] < 0]
    top_longs = _enrich_with_catalyst(long_pool, catalyst_map, side="LONG")[:5]
    top_shorts = _enrich_with_catalyst(short_pool, catalyst_map, side="SHORT")[:5]
    backtest_targets = [(row, "LONG") for row in top_longs] + [
        (row, "SHORT") for row in top_shorts
    ]

    msg = (
        "**[Value Reversion Scan Report]**\n"
        "=============================\n\n"
        "**Scoring**\n"
        "Aggressive test setting: value reversion score 50% + catalyst factor 50%.\n"
        f"Suggested holding period: {REVERSION_HOLDING_DAYS} trading days.\n"
        "For SHORT candidates, positive catalyst reduces rank.\n\n"
        "**Hypothesis**\n"
        "Stocks whose cap/equity ratio has recently fallen versus their own "
        "short-term history may be temporarily undervalued and likely to rebound.\n"
        "Stocks whose cap/equity ratio has recently risen too much may be "
        "overheated and vulnerable to reversal.\n\n"
        f"Scored universe: {len(scored)} / {len(universe_list)} tickers\n"
        f"Data issues: {len(failed)} tickers\n\n"
        "**LONG Candidates**\n"
        f"{_format_rows(top_longs, long_side=True)}\n"
        "**SHORT Candidates**\n"
        f"{_format_rows(top_shorts, long_side=False)}"
        f"{_format_backtest(backtest_targets)}"
    )

    if failed:
        msg += (
            "\n----------------------------------------\n"
            "Use Admin -> Refresh Selected Universe if coverage looks unusually low."
        )

    return msg
