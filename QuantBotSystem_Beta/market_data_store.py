# File name: market_data_store.py
# Caches OHLCV price history in SQLite and serves strategy-ready data frames.
import os
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "market_data.sqlite")
RECENT_REFRESH_ROWS = 7
DOWNLOAD_SLEEP_SECONDS = 0.7


def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            adj_close REAL,
            volume REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv(ticker, date)")
    return conn


def _normalize_download(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        return pd.DataFrame()
    if "Adj Close" not in df.columns:
        df["Adj Close"] = df["Close"]
    if "Volume" not in df.columns:
        df["Volume"] = 0
    df = df.dropna(subset=required).copy()
    df.index = pd.to_datetime(df.index).date
    return df


def _upsert_ohlcv(conn, ticker, df):
    if df.empty:
        return 0

    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for date_value, row in df.iterrows():
        rows.append(
            (
                ticker,
                date_value.isoformat(),
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Adj Close"]),
                float(row["Volume"]) if pd.notna(row["Volume"]) else 0.0,
                updated_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO ohlcv (
            ticker, date, open, high, low, close, adj_close, volume, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            adj_close=excluded.adj_close,
            volume=excluded.volume,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _download_history(ticker, period):
    return _normalize_download(
        yf.download(ticker, period=period, progress=False, auto_adjust=False)
    )


def _get_cached_row_count(conn, ticker):
    row = conn.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = ?", (ticker,)).fetchone()
    return int(row[0]) if row else 0


def _get_latest_cached_date(conn, ticker):
    row = conn.execute("SELECT MAX(date) FROM ohlcv WHERE ticker = ?", (ticker,)).fetchone()
    return row[0] if row and row[0] else None


def ensure_price_data(
    tickers,
    min_period="1y",
    refresh_recent=True,
    sleep_seconds=DOWNLOAD_SLEEP_SECONDS,
    progress_callback=None,
):
    """Ensure selected tickers have local cached data before a scan."""
    tickers = sorted(set(tickers))
    summary = {
        "requested": len(tickers),
        "updated": 0,
        "failed": [],
        "latest_dates": {},
        "db_path": DB_PATH,
    }
    if not tickers:
        return summary

    conn = get_connection()
    try:
        for idx, ticker in enumerate(tickers, start=1):
            try:
                if progress_callback:
                    progress_callback(idx, len(tickers), ticker)
                cached_rows = _get_cached_row_count(conn, ticker)
                period = min_period if cached_rows == 0 else "1mo"
                downloaded = _download_history(ticker, period=period)

                if downloaded.empty and cached_rows == 0:
                    summary["failed"].append(ticker)
                    continue

                if not downloaded.empty:
                    if cached_rows > 0 and refresh_recent:
                        downloaded = downloaded.tail(RECENT_REFRESH_ROWS)
                    summary["updated"] += _upsert_ohlcv(conn, ticker, downloaded)

                latest_date = _get_latest_cached_date(conn, ticker)
                if latest_date:
                    summary["latest_dates"][ticker] = latest_date
            except Exception:
                if _get_cached_row_count(conn, ticker) == 0:
                    summary["failed"].append(ticker)
            if sleep_seconds and idx < len(tickers):
                time.sleep(sleep_seconds)
    finally:
        conn.close()

    return summary


def backfill_price_data(
    tickers,
    period="2y",
    sleep_seconds=DOWNLOAD_SLEEP_SECONDS,
    progress_callback=None,
):
    """Download a full history window even when a ticker already has recent rows."""
    tickers = sorted(set(tickers))
    summary = {
        "requested": len(tickers),
        "updated": 0,
        "failed": [],
        "latest_dates": {},
        "db_path": DB_PATH,
    }
    if not tickers:
        return summary

    conn = get_connection()
    try:
        for idx, ticker in enumerate(tickers, start=1):
            try:
                if progress_callback:
                    progress_callback(idx, len(tickers), ticker)
                downloaded = _download_history(ticker, period=period)
                if downloaded.empty:
                    summary["failed"].append(ticker)
                else:
                    summary["updated"] += _upsert_ohlcv(conn, ticker, downloaded)
                latest_date = _get_latest_cached_date(conn, ticker)
                if latest_date:
                    summary["latest_dates"][ticker] = latest_date
            except Exception:
                summary["failed"].append(ticker)
            if sleep_seconds and idx < len(tickers):
                time.sleep(sleep_seconds)
    finally:
        conn.close()

    return summary


def preload_all_universe(config_module, progress_callback=None):
    tickers = sorted(set(config_module.universe))
    return ensure_price_data(
        tickers,
        min_period="2y",
        refresh_recent=True,
        progress_callback=progress_callback,
    )


def format_cache_summary(summary, title="Market Data Update Complete"):
    failed = ", ".join(summary["failed"]) if summary["failed"] else "None"
    latest_dates = sorted(set(summary["latest_dates"].values()))
    if not latest_dates:
        latest_text = "N/A"
    elif len(latest_dates) == 1:
        latest_text = latest_dates[0]
    else:
        latest_text = f"{latest_dates[0]} to {latest_dates[-1]}"

    return (
        f"**{title}**\n\n"
        f"Tickers requested: {summary['requested']}\n"
        f"Rows updated: {summary['updated']}\n"
        f"Latest date: {latest_text}\n"
        f"Failed: {failed}\n"
        f"DB path: `{summary['db_path']}`"
    )


def get_price_data(ticker, period_days=None):
    """Read cached OHLCV data as a yfinance-like DataFrame."""
    conn = get_connection()
    try:
        query = """
            SELECT date, open, high, low, close, adj_close, volume
            FROM ohlcv
            WHERE ticker = ?
            ORDER BY date
        """
        df = pd.read_sql_query(query, conn, params=(ticker,))
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "adj_close": "Adj Close",
            "volume": "Volume",
        }
    )
    if period_days is not None and len(df) > period_days:
        df = df.tail(period_days)
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]


def get_data_freshness(tickers):
    conn = get_connection()
    freshness = {}
    try:
        for ticker in sorted(set(tickers)):
            latest_date = _get_latest_cached_date(conn, ticker)
            if latest_date:
                freshness[ticker] = latest_date
    finally:
        conn.close()
    return freshness


def get_missing_tickers(tickers):
    freshness = get_data_freshness(tickers)
    return [ticker for ticker in sorted(set(tickers)) if ticker not in freshness]


def freshness_summary(tickers):
    freshness = get_data_freshness(tickers)
    if not freshness:
        return "Data cache: empty"

    latest_dates = sorted(set(freshness.values()))
    if len(latest_dates) == 1:
        return f"Data cache latest date: {latest_dates[0]}"
    return f"Data cache latest date range: {latest_dates[0]} to {latest_dates[-1]}"
