# File name: value_reversion_engine.py
# Ranks cap-to-equity mean reversion signals using cached fundamentals.
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

import market_data_store


FUNDAMENTAL_TTL_DAYS = 30
ZSCORE_WINDOW = 5


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


def analyze_value_reversion(ticker, allow_fundamental_download=True):
    prices = market_data_store.get_price_data(ticker, period_days=40)
    if prices.empty or len(prices) < ZSCORE_WINDOW:
        return {"status": "DATA_INSUFFICIENT", "score": 0.0}

    fundamentals = get_fundamentals(ticker, allow_download=allow_fundamental_download)
    if not fundamentals:
        return {"status": "FUNDAMENTAL_MISSING", "score": 0.0}

    ratio = prices["Close"].astype(float) * fundamentals["shares_outstanding"]
    ratio = ratio / fundamentals["total_equity"]
    zscore = _zscore_latest(ratio)
    if zscore is None:
        return {"status": "NO_SIGNAL", "score": 0.0}

    score = -zscore
    latest_ratio = float(ratio.iloc[-1])
    if latest_ratio <= 0.05 or latest_ratio >= 500:
        return {"status": "DATA_SUSPECT", "score": 0.0}

    return {
        "status": "OK",
        "score": score,
        "cap_to_equity": latest_ratio,
        "updated_at": fundamentals.get("updated_at"),
    }


def _format_rows(items, long_side=True):
    if not items:
        return "None\n"
    rows = []
    for idx, row in enumerate(items, start=1):
        direction = "LONG" if long_side else "SHORT"
        rows.append(
            f"{idx}. {row['ticker']:<8} | {direction} | "
            f"score {row['score']:+.2f} | cap/equity {row['cap_to_equity']:.2f}"
        )
    return "\n".join(rows) + "\n"


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
            }
        )

    scored.sort(key=lambda row: row["score"], reverse=True)
    top_longs = scored[:5]
    top_shorts = list(reversed(scored[-5:])) if len(scored) >= 5 else []

    msg = (
        "**[Value Reversion Scan Report]**\n"
        "=============================\n\n"
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
    )

    if failed:
        msg += (
            "\n----------------------------------------\n"
            "Use Admin -> Refresh Selected Universe if coverage looks unusually low."
        )

    return msg
