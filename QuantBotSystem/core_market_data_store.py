# File name: market_data_store.py
# Caches OHLCV price history in SQLite and serves strategy-ready data frames.
import os
import sqlite3
import time
from datetime import datetime, timezone
from math import ceil, isfinite

import pandas as pd
import yfinance as yf

import core_backtest_periods
import runtime_settings


BASE_DIR = str(runtime_settings.BASE_DIR)
DATA_DIR = str(runtime_settings.DATA_DIR)
DB_PATH = str(runtime_settings.MARKET_DB_PATH)
INCREMENTAL_OVERLAP_CALENDAR_DAYS = 10
DOWNLOAD_SLEEP_SECONDS = 0.7
METADATA_SLEEP_SECONDS = 0.3
FIXED_IS_OOS_HISTORY_PERIOD = "4y"
MARKET_BENCHMARK_TICKER = "ACWI"
MARKET_BENCHMARK_NAME = "ACWI"
MARKET_BENCHMARK_DOWNLOAD_START = (
    pd.Timestamp(core_backtest_periods.SELECTION_START) - pd.Timedelta(days=31)
).date().isoformat()
MARKET_BENCHMARK_MAX_STALE_CALENDAR_DAYS = (
    core_backtest_periods.BENCHMARK_MAX_STALE_CALENDAR_DAYS
)
MARKET_BENCHMARK_MIN_BUSINESS_DAY_COVERAGE = 0.75
RESERVED_BENCHMARK_TICKERS = frozenset(
    {
        MARKET_BENCHMARK_TICKER,
        "MIWD00000PUS",  # Keep legacy exact-index rows out of security universes.
    }
)


def get_connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
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


def _exclude_reserved_benchmark_tickers(tickers):
    return [
        ticker
        for ticker in tickers
        if str(ticker).upper() not in RESERVED_BENCHMARK_TICKERS
    ]


def refresh_listing_country_labels_from_cache(tickers=None):
    """Populate US/CH/JP/KR listing-market labels from cached ticker symbols only."""
    conn = get_connection()
    try:
        if tickers is None:
            tickers = [row[0] for row in conn.execute(
                "SELECT DISTINCT ticker FROM ohlcv ORDER BY ticker"
            ).fetchall()]
        tickers = sorted(set(_exclude_reserved_benchmark_tickers(tickers)))
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
    tickers = sorted(set(_exclude_reserved_benchmark_tickers(tickers)))
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


def _normalize_download(df, synthesize_adj_close=True):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        return pd.DataFrame()
    if "Adj Close" not in df.columns and synthesize_adj_close:
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


def _report_benchmark_refresh_progress(progress_callback, completed, stage_label):
    if progress_callback is not None:
        progress_callback(completed, 4, stage_label)


def refresh_market_benchmark(progress_callback=None, as_of=None):
    """Refresh ACWI from the fixed pre-IS seed through ``as_of``.

    A full refresh is deliberate: a newly paid distribution can cause Yahoo to
    restate older adjusted closes, which a short incremental overlap would miss.
    """
    try:
        as_of_date = pd.Timestamp(
            as_of if as_of is not None else datetime.now(timezone.utc).date()
        ).date()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("as_of must be a valid date") from exc
    download_end = (pd.Timestamp(as_of_date) + pd.Timedelta(days=1)).date().isoformat()

    _report_benchmark_refresh_progress(
        progress_callback, 1, "ACWI 다운로드 준비"
    )
    _report_benchmark_refresh_progress(
        progress_callback, 2, "ACWI 가격 다운로드"
    )
    try:
        downloaded = _download_market_benchmark_history(
            MARKET_BENCHMARK_TICKER,
            start=MARKET_BENCHMARK_DOWNLOAD_START,
            end=download_end,
        )
    except Exception as exc:
        raise RuntimeError(
            "ACWI benchmark refresh failed. Yahoo returned no usable OHLCV data; "
            "the existing cache was not replaced."
        ) from exc

    _report_benchmark_refresh_progress(
        progress_callback, 3, "ACWI Adj Close 검증"
    )
    required_columns = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
    if downloaded is None or downloaded.empty:
        raise RuntimeError(
            "ACWI benchmark refresh failed. Yahoo returned no usable OHLCV data; "
            "the existing cache was not replaced."
        )
    if downloaded.columns.duplicated().any():
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded columns are duplicated; "
            "the existing cache was not replaced."
        )
    missing_columns = [
        column for column in required_columns if column not in downloaded.columns
    ]
    if missing_columns:
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded OHLCV is missing "
            f"{', '.join(missing_columns)}; the existing cache was not replaced."
        )

    validated = downloaded.loc[:, list(required_columns)].copy()
    try:
        normalized_dates = pd.to_datetime(validated.index, errors="raise")
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded dates are invalid; "
            "the existing cache was not replaced."
        ) from exc
    if pd.isna(normalized_dates).any():
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded dates are invalid; "
            "the existing cache was not replaced."
        )
    normalized_dates = pd.Index([pd.Timestamp(value).date() for value in normalized_dates])
    if normalized_dates.duplicated().any():
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded dates are duplicated; "
            "the existing cache was not replaced."
        )
    validated.index = normalized_dates
    validated = validated.sort_index()

    for adj_close in validated["Adj Close"]:
        try:
            parsed_adj_close = float(adj_close)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "ACWI benchmark refresh failed: Adj Close must be finite and "
                "positive for every row; the existing cache was not replaced."
            ) from exc
        if not isfinite(parsed_adj_close) or parsed_adj_close <= 0:
            raise RuntimeError(
                "ACWI benchmark refresh failed: Adj Close must be finite and "
                "positive for every row; the existing cache was not replaced."
            )

    required_start = pd.Timestamp(core_backtest_periods.SELECTION_START).date()
    first_date = validated.index[0]
    latest_date = validated.index[-1]
    if first_date >= required_start:
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded history has no pre-IS "
            f"seed before {required_start}; the existing cache was not replaced."
        )
    seed_date = max(
        date_value for date_value in validated.index if date_value < required_start
    )
    seed_stale_days = (required_start - seed_date).days
    if seed_stale_days > MARKET_BENCHMARK_MAX_STALE_CALENDAR_DAYS:
        raise RuntimeError(
            "ACWI benchmark refresh failed: the pre-IS seed is "
            f"{seed_stale_days} calendar days stale; "
            "the existing cache was not replaced."
        )
    stale_days = (as_of_date - latest_date).days
    if stale_days > MARKET_BENCHMARK_MAX_STALE_CALENDAR_DAYS:
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded history is "
            f"{stale_days} calendar days stale; the existing cache was not replaced."
        )

    coverage_end = min(as_of_date, latest_date)
    expected_business_days = len(
        pd.bdate_range(required_start, coverage_end)
    )
    actual_business_days = sum(
        required_start <= date_value <= coverage_end
        for date_value in validated.index
    )
    minimum_rows = ceil(
        expected_business_days * MARKET_BENCHMARK_MIN_BUSINESS_DAY_COVERAGE
    )
    if expected_business_days and actual_business_days < minimum_rows:
        raise RuntimeError(
            "ACWI benchmark refresh failed: downloaded history appears partial "
            f"({actual_business_days}/{expected_business_days} business-day rows); "
            "the existing cache was not replaced."
        )

    conn = get_connection()
    try:
        try:
            conn.execute(
                "DELETE FROM ohlcv WHERE ticker = ?",
                (MARKET_BENCHMARK_TICKER,),
            )
            updated_rows = _upsert_ohlcv(
                conn,
                MARKET_BENCHMARK_TICKER,
                validated,
            )
        except Exception:
            conn.rollback()
            raise
        cached_rows = conn.execute(
            """
            SELECT date, adj_close
            FROM ohlcv
            WHERE ticker = ?
            ORDER BY date
            """,
            (MARKET_BENCHMARK_TICKER,),
        ).fetchall()
    finally:
        conn.close()

    if not cached_rows:
        raise RuntimeError(
            "ACWI benchmark refresh produced no cached rows; "
            "the benchmark remains unavailable."
        )

    for date_value, adj_close in cached_rows:
        try:
            parsed_date = pd.Timestamp(date_value)
            parsed_adj_close = float(adj_close)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                "ACWI benchmark cache validation failed: invalid date or Adj Close."
            ) from exc
        if pd.isna(parsed_date) or not isfinite(parsed_adj_close) or parsed_adj_close <= 0:
            raise RuntimeError(
                "ACWI benchmark cache validation failed: Adj Close must be "
                "finite and positive for every row."
            )

    first_date = str(cached_rows[0][0])
    latest_date = str(cached_rows[-1][0])
    summary = {
        "mode": "fixed_start_full_history",
        "requested": 1,
        "updated": updated_rows,
        "full_downloads": 1,
        "incremental_downloads": 0,
        "unchanged": 0,
        "failed": [],
        "latest_dates": {MARKET_BENCHMARK_TICKER: latest_date},
        "db_path": DB_PATH,
        "ticker": MARKET_BENCHMARK_TICKER,
        "benchmark_name": MARKET_BENCHMARK_NAME,
        "source_kind": "yfinance",
        "data_basis": "Adj Close",
        "cached_rows": len(cached_rows),
        "cached_first_date": first_date,
        "cached_latest_date": latest_date,
        "first_date": first_date,
        "latest_date": latest_date,
    }
    _report_benchmark_refresh_progress(
        progress_callback, 4, "ACWI DB 확인 완료"
    )
    return summary


def format_market_benchmark_refresh_summary(summary):
    return (
        "ACWI Benchmark Refresh Complete\n\n"
        "Benchmark: ACWI\n"
        "Source: Yahoo Finance\n"
        "Data basis: Adj Close\n"
        f"Rows received/upserted: {summary['updated']}\n"
        f"Cached range: {summary['cached_first_date']} to "
        f"{summary['cached_latest_date']} ({summary['cached_rows']} rows)\n"
        f"DB path: {summary['db_path']}"
    )


def _download_history(ticker, period):
    return _normalize_download(
        yf.download(ticker, period=period, progress=False, auto_adjust=False)
    )


def _download_history_range(ticker, start, end):
    """Download an explicit date range; yfinance treats end as exclusive."""
    return _normalize_download(
        yf.download(
            ticker,
            start=pd.Timestamp(start).date().isoformat(),
            end=pd.Timestamp(end).date().isoformat(),
            progress=False,
            auto_adjust=False,
        )
    )


def _download_market_benchmark_history(ticker, start, end):
    """Download benchmark history without fabricating a missing adjusted close."""
    return _normalize_download(
        yf.download(
            ticker,
            start=pd.Timestamp(start).date().isoformat(),
            end=pd.Timestamp(end).date().isoformat(),
            progress=False,
            auto_adjust=False,
        ),
        synthesize_adj_close=False,
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
    """Ensure data using an incremental latest-date refresh for cached tickers."""
    overlap_days = INCREMENTAL_OVERLAP_CALENDAR_DAYS if refresh_recent else 0
    return refresh_latest_price_data(
        tickers,
        min_period=min_period,
        overlap_days=overlap_days,
        sleep_seconds=sleep_seconds,
        progress_callback=progress_callback,
    )


def refresh_latest_price_data(
    tickers,
    min_period=FIXED_IS_OOS_HISTORY_PERIOD,
    overlap_days=INCREMENTAL_OVERLAP_CALENDAR_DAYS,
    sleep_seconds=DOWNLOAD_SLEEP_SECONDS,
    progress_callback=None,
    as_of=None,
):
    """Fill the latest tail from each ticker's cached max date with overlap.

    Tickers without any cache receive ``min_period`` history. Cached tickers use
    an explicit start/end request from the latest date minus ``overlap_days`` to
    the day after ``as_of`` because yfinance's end date is exclusive.
    """
    tickers = sorted(set(tickers))
    as_of_date = pd.Timestamp(
        as_of if as_of is not None else datetime.now(timezone.utc).date()
    ).normalize()
    summary = {
        "mode": "incremental",
        "requested": len(tickers),
        "updated": 0,
        "full_downloads": 0,
        "incremental_downloads": 0,
        "unchanged": 0,
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
                latest_date = _get_latest_cached_date(conn, ticker)
                if cached_rows == 0 or not latest_date:
                    summary["full_downloads"] += 1
                    downloaded = _download_history(ticker, period=min_period)
                else:
                    summary["incremental_downloads"] += 1
                    start_date = pd.Timestamp(latest_date) - pd.Timedelta(
                        days=max(int(overlap_days), 0)
                    )
                    end_date = as_of_date + pd.Timedelta(days=1)
                    if start_date > end_date:
                        start_date = end_date - pd.Timedelta(days=1)
                    downloaded = _download_history_range(
                        ticker, start=start_date, end=end_date
                    )

                if downloaded.empty and cached_rows == 0:
                    summary["failed"].append(ticker)
                    continue

                if not downloaded.empty:
                    summary["updated"] += _upsert_ohlcv(conn, ticker, downloaded)
                else:
                    summary["unchanged"] += 1

                latest_date = _get_latest_cached_date(conn, ticker)
                if latest_date:
                    summary["latest_dates"][ticker] = latest_date
            except Exception:
                if ticker not in summary["failed"]:
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
        "mode": "full_history",
        "requested": len(tickers),
        "updated": 0,
        "full_downloads": len(tickers),
        "incremental_downloads": 0,
        "unchanged": 0,
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

    mode = summary.get("mode")
    mode_line = (
        "Mode: Latest incremental refresh\n"
        if mode == "incremental"
        else "Mode: Full history backfill\n"
        if mode == "full_history"
        else ""
    )
    download_line = ""
    if "incremental_downloads" in summary:
        download_line = (
            f"Incremental tickers: {summary.get('incremental_downloads', 0)}\n"
            f"Full-history tickers: {summary.get('full_downloads', 0)}\n"
            f"No new rows: {summary.get('unchanged', 0)}\n"
        )
    return (
        f"**{title}**\n\n"
        f"{mode_line}"
        f"Tickers requested: {summary['requested']}\n"
        f"{download_line}"
        f"Rows received/upserted: {summary['updated']}\n"
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
    tickers = sorted(set(tickers))
    if not tickers:
        return {}

    conn = get_connection()
    try:
        placeholders = ", ".join("?" for _ in tickers)
        rows = conn.execute(
            f"""
            SELECT ticker, MAX(date)
            FROM ohlcv
            WHERE ticker IN ({placeholders})
            GROUP BY ticker
            """,
            tickers,
        ).fetchall()
    finally:
        conn.close()
    return {ticker: latest_date for ticker, latest_date in rows if latest_date}


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
