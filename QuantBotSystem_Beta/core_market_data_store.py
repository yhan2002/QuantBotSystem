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
METADATA_SLEEP_SECONDS = 0.3
FIXED_IS_OOS_HISTORY_PERIOD = "4y"


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS security_metadata (
            ticker TEXT PRIMARY KEY,
            listing_country TEXT,
            company_country TEXT,
            sector TEXT,
            industry TEXT,
            exchange TEXT,
            quote_type TEXT,
            short_name TEXT,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def _infer_listing_country(ticker):
    """Map the ticker's listing market into the bot's four-region universe."""
    ticker = str(ticker).upper()
    if ticker.endswith(".T"):
        return "JP"
    if ticker.endswith((".SS", ".SZ", ".HK")):
        return "CH"
    if ticker.endswith((".KS", ".KQ")):
        return "KR"
    return "US"


def refresh_listing_country_labels_from_cache(tickers=None):
    """Populate US/CH/JP/KR listing-market labels from cached ticker symbols only."""
    conn = get_connection()
    try:
        if tickers is None:
            tickers = [row[0] for row in conn.execute(
                "SELECT DISTINCT ticker FROM ohlcv ORDER BY ticker"
            ).fetchall()]
        tickers = sorted(set(tickers))
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            (ticker, _infer_listing_country(ticker), "ticker_suffix", updated_at)
            for ticker in tickers
        ]
        conn.executemany(
            """
            INSERT INTO security_metadata (ticker, listing_country, source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                listing_country=excluded.listing_country,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    groups = {}
    for _, listing_country, _, _ in rows:
        groups[listing_country] = groups.get(listing_country, 0) + 1
    return {
        "requested": len(rows),
        "updated": len(rows),
        "groups": groups,
        "db_path": DB_PATH,
    }


def _download_security_metadata(ticker):
    try:
        info = yf.Ticker(ticker).get_info() or {}
    except Exception as exc:
        if "invalid crumb" not in str(exc).lower():
            raise
        _reset_yfinance_cookie_session()
        info = yf.Ticker(ticker).get_info() or {}
    return {
        "listing_country": _infer_listing_country(ticker),
        "company_country": info.get("country"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "exchange": info.get("exchange"),
        "quote_type": info.get("quoteType"),
        "short_name": info.get("shortName") or info.get("longName"),
    }


def _reset_yfinance_cookie_session():
    """Clear stale Yahoo cookie/crumb state once before retrying metadata."""
    try:
        cookie_cache = yf.cache.get_cookie_cache()
        for strategy in ("basic", "csrf"):
            cookie_cache.store(strategy, None)
    except Exception:
        pass
    try:
        from yfinance.data import YfData

        data = YfData()
        data._cookie = None
        data._crumb = None
        data._set_cookie_strategy("basic")
    except Exception:
        pass


def _upsert_security_metadata(conn, ticker, metadata):
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO security_metadata (
            ticker, listing_country, company_country, sector, industry,
            exchange, quote_type, short_name, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            listing_country=excluded.listing_country,
            company_country=excluded.company_country,
            sector=excluded.sector,
            industry=excluded.industry,
            exchange=excluded.exchange,
            quote_type=excluded.quote_type,
            short_name=excluded.short_name,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (
            ticker,
            metadata.get("listing_country"),
            metadata.get("company_country"),
            metadata.get("sector"),
            metadata.get("industry"),
            metadata.get("exchange"),
            metadata.get("quote_type"),
            metadata.get("short_name"),
            "yfinance",
            updated_at,
        ),
    )
    conn.commit()


def refresh_security_metadata(
    tickers,
    sleep_seconds=METADATA_SLEEP_SECONDS,
    progress_callback=None,
):
    """Download and cache country/sector/industry labels independently of prices."""
    tickers = sorted(set(tickers))
    summary = {
        "requested": len(tickers),
        "updated": 0,
        "failed": [],
        "skipped": 0,
        "auth_error": False,
        "db_path": DB_PATH,
    }
    if not tickers:
        return summary

    market_country = refresh_listing_country_labels_from_cache(tickers)
    summary["market_country_updated"] = market_country["updated"]

    conn = get_connection()
    try:
        for idx, ticker in enumerate(tickers, start=1):
            try:
                if progress_callback:
                    progress_callback(idx, len(tickers), ticker)
                metadata = _download_security_metadata(ticker)
                _upsert_security_metadata(conn, ticker, metadata)
                summary["updated"] += 1
            except Exception as exc:
                summary["failed"].append(ticker)
                if "invalid crumb" in str(exc).lower():
                    summary["auth_error"] = True
                    summary["skipped"] = len(tickers) - idx
                    break
            if sleep_seconds and idx < len(tickers):
                time.sleep(sleep_seconds)
    finally:
        conn.close()
    return summary


def get_security_metadata(tickers=None):
    """Return cached classification rows keyed by ticker."""
    conn = get_connection()
    try:
        if tickers is None:
            rows = conn.execute(
                """SELECT ticker, listing_country, company_country, sector, industry,
                          exchange, quote_type, short_name, source, updated_at
                   FROM security_metadata ORDER BY ticker"""
            ).fetchall()
        else:
            tickers = sorted(set(tickers))
            if not tickers:
                return {}
            placeholders = ",".join("?" for _ in tickers)
            rows = conn.execute(
                f"""SELECT ticker, listing_country, company_country, sector, industry,
                           exchange, quote_type, short_name, source, updated_at
                    FROM security_metadata WHERE ticker IN ({placeholders}) ORDER BY ticker""",
                tickers,
            ).fetchall()
    finally:
        conn.close()

    fields = (
        "ticker", "listing_country", "company_country", "sector", "industry",
        "exchange", "quote_type", "short_name", "source", "updated_at",
    )
    return {row[0]: dict(zip(fields, row)) for row in rows}


def format_metadata_summary(summary):
    failed = summary["failed"]
    failed_text = ", ".join(failed[:10]) if failed else "None"
    if len(failed) > 10:
        failed_text += f" ... +{len(failed) - 10} more"
    report = (
        "**Security Classifications Refresh Complete**\n\n"
        f"Tickers requested: {summary['requested']}\n"
        f"Market-country labels updated: {summary.get('market_country_updated', 0)}\n"
        f"Metadata updated: {summary['updated']}\n"
        f"Failed: {len(failed)} ({failed_text})\n"
        f"Skipped: {summary.get('skipped', 0)}\n"
        f"DB path: `{summary['db_path']}`"
    )
    if summary.get("auth_error"):
        report += (
            "\n\nYahoo authentication rejected its crumb after one reset/retry. "
            "Wait a few minutes, restart the bot, then run Refresh Sector / Industry again."
        )
    return report


def format_market_country_summary(summary):
    groups = " | ".join(
        f"{country} {count}" for country, count in sorted(summary["groups"].items())
    ) or "None"
    return (
        "**Listing-Market Country Refresh Complete**\n\n"
        f"Tickers classified: {summary['updated']}\n"
        f"Groups: {groups}\n"
        "Rule: .HK/.SS/.SZ → CH, .T → JP, .KS/.KQ → KR, all other tickers → US\n"
        f"DB path: `{summary['db_path']}`"
    )


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


def preload_all_universe(config_module, progress_callback=None, full_history=False):
    tickers = sorted(set(config_module.universe))
    if full_history:
        return backfill_price_data(
            tickers,
            period=FIXED_IS_OOS_HISTORY_PERIOD,
            progress_callback=progress_callback,
        )
    return ensure_price_data(
        tickers,
        min_period=FIXED_IS_OOS_HISTORY_PERIOD,
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
