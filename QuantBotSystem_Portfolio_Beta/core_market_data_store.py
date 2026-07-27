# File name: market_data_store.py
# Caches OHLCV price history in SQLite and serves strategy-ready data frames.
import io
import os
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pandas as pd
import yfinance as yf

import runtime_settings


BASE_DIR = str(runtime_settings.BASE_DIR)
DATA_DIR = str(runtime_settings.DATA_DIR)
DB_PATH = str(runtime_settings.MARKET_DB_PATH)
INCREMENTAL_OVERLAP_CALENDAR_DAYS = 10
DOWNLOAD_SLEEP_SECONDS = 0.7
METADATA_SLEEP_SECONDS = 0.3
FIXED_IS_OOS_HISTORY_PERIOD = "4y"
MARKET_BENCHMARK_TICKER = "MIWD00000PUS"
MARKET_BENCHMARK_NAME = "MSCI ACWI"
MARKET_BENCHMARK_MAX_CSV_BYTES = 5 * 1024 * 1024
RESERVED_BENCHMARK_TICKERS = frozenset({MARKET_BENCHMARK_TICKER})


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


def upsert_close_series(ticker, frame):
    """Atomically insert or update a validated Date/Close series.

    Existing OHLCV fields are deliberately left unchanged on conflicts. The
    complete input is normalized and validated before the database is opened,
    so malformed input cannot produce a partial import.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker must be a non-empty string")
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    if frame.columns.duplicated().any():
        raise ValueError("frame columns must be unique")

    missing = [column for column in ("Date", "Close") if column not in frame.columns]
    if missing:
        raise ValueError(f"frame is missing required columns: {', '.join(missing)}")
    if frame.empty:
        raise ValueError("frame must contain at least one row")

    dates = []
    closes = []
    for row_number, (date_value, close_value) in enumerate(
        frame.loc[:, ["Date", "Close"]].itertuples(index=False, name=None),
        start=1,
    ):
        try:
            parsed_date = pd.Timestamp(date_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid Date at row {row_number}: {date_value!r}") from exc
        if pd.isna(parsed_date):
            raise ValueError(f"invalid Date at row {row_number}: {date_value!r}")

        try:
            parsed_close = float(close_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"invalid Close at row {row_number}: {close_value!r}"
            ) from exc
        if not isfinite(parsed_close) or parsed_close <= 0:
            raise ValueError(
                f"Close must be finite and positive at row {row_number}: "
                f"{close_value!r}"
            )

        dates.append(parsed_date.date().isoformat())
        closes.append(parsed_close)

    duplicated_dates = pd.Series(dates).duplicated(keep=False)
    if duplicated_dates.any():
        duplicates = sorted(set(pd.Series(dates)[duplicated_dates].tolist()))
        raise ValueError(f"duplicate Date values after normalization: {', '.join(duplicates)}")

    normalized_ticker = ticker.strip().upper()
    normalized_rows = sorted(zip(dates, closes))
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (normalized_ticker, date_value, close_value, updated_at)
        for date_value, close_value in normalized_rows
    ]

    conn = get_connection()
    try:
        conn.execute("BEGIN")
        conn.executemany(
            """
            INSERT INTO ohlcv (ticker, date, close, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                close=excluded.close,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "ticker": normalized_ticker,
        "updated": len(rows),
        "first_date": normalized_rows[0][0],
        "latest_date": normalized_rows[-1][0],
        "db_path": DB_PATH,
    }


def _report_benchmark_refresh_progress(progress_callback, completed, stage_label):
    if progress_callback is not None:
        progress_callback(completed, 4, stage_label)


def _normalize_benchmark_source_path(source_path):
    raw_path = os.path.expandvars(os.path.expanduser(os.fspath(source_path)))
    return os.path.abspath(raw_path)


def _safe_https_source_display(source_url):
    parsed = urlsplit(str(source_url).strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("MSCI ACWI benchmark source URL must use HTTPS")
    if not parsed.hostname:
        raise ValueError("MSCI ACWI benchmark source URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MSCI ACWI benchmark source URL must not contain credentials")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MSCI ACWI benchmark source URL has an invalid port") from exc
    port_suffix = f":{port}" if port is not None else ""
    return f"https://{host}{port_suffix}"


def _https_source_origin(source_url):
    parsed = urlsplit(str(source_url).strip())
    _safe_https_source_display(source_url)
    default_port = 443
    return parsed.hostname.lower(), parsed.port or default_port


class _SameOriginHttpsRedirectHandler(HTTPRedirectHandler):
    def __init__(self, source_origin):
        super().__init__()
        self._source_origin = source_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _https_source_origin(newurl) != self._source_origin:
            raise ValueError(
                "MSCI ACWI benchmark source redirected to a different host"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_limited_https_csv(source_url, source_headers, timeout_seconds, opener):
    safe_display = _safe_https_source_display(source_url)
    source_origin = _https_source_origin(source_url)
    if not isinstance(source_headers, Mapping):
        raise TypeError("MSCI_ACWI_SOURCE_HEADERS must be a mapping")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds must be a positive number") from exc
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive number")

    headers = {}
    for raw_name, raw_value in source_headers.items():
        name = str(raw_name)
        value = str(raw_value)
        if (
            not name
            or "\r" in name
            or "\n" in name
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError(
                "MSCI_ACWI_SOURCE_HEADERS contains an invalid header"
            )
        headers[name] = value
    headers.setdefault("User-Agent", "QuantBot-MSCI-ACWI-Refresh/1.0")
    request = Request(str(source_url).strip(), headers=headers, method="GET")
    open_url = opener
    if open_url is None:
        open_url = build_opener(
            _SameOriginHttpsRedirectHandler(source_origin)
        ).open
    try:
        with open_url(request, timeout=timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else source_url
            if _https_source_origin(final_url) != source_origin:
                raise ValueError(
                    "MSCI ACWI benchmark source redirected to a different host"
                )

            content_length = None
            response_headers = getattr(response, "headers", None)
            if response_headers is not None:
                content_length = response_headers.get("Content-Length")
            if content_length not in (None, ""):
                try:
                    declared_bytes = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "benchmark response has an invalid Content-Length"
                    ) from exc
                if declared_bytes > MARKET_BENCHMARK_MAX_CSV_BYTES:
                    raise ValueError("benchmark CSV response exceeds the 5 MB limit")

            payload = response.read(MARKET_BENCHMARK_MAX_CSV_BYTES + 1)
    except HTTPError as exc:
        status_code = exc.code
        exc.close()
        if status_code in {401, 403}:
            raise RuntimeError(
                f"MSCI ACWI refresh unavailable (HTTP {status_code}). "
                "Verify MSCI entitlement and local credentials; "
                "the existing cache was not changed."
            ) from None
        raise RuntimeError(
            f"MSCI ACWI HTTPS source failed (HTTP {status_code}); "
            "the existing cache was not changed."
        ) from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError(
            f"MSCI ACWI HTTPS source could not be reached at {safe_display}; "
            "the existing cache was not changed."
        ) from None
    if len(payload) > MARKET_BENCHMARK_MAX_CSV_BYTES:
        raise ValueError("benchmark CSV response exceeds the 5 MB limit")
    if not payload:
        raise ValueError("benchmark CSV response is empty")
    return payload, safe_display


def _read_limited_local_csv(source_path):
    normalized_path = _normalize_benchmark_source_path(source_path)
    if not os.path.isfile(normalized_path):
        raise FileNotFoundError(
            "MSCI ACWI benchmark CSV was not found. "
            f"Place a Date/Close CSV at {normalized_path} or configure "
            "MSCI_ACWI_SOURCE_PATH / MSCI_ACWI_SOURCE_URL in config.local.py."
        )
    if os.path.getsize(normalized_path) > MARKET_BENCHMARK_MAX_CSV_BYTES:
        raise ValueError("benchmark CSV file exceeds the 5 MB limit")
    with open(normalized_path, "rb") as csv_file:
        payload = csv_file.read(MARKET_BENCHMARK_MAX_CSV_BYTES + 1)
    if len(payload) > MARKET_BENCHMARK_MAX_CSV_BYTES:
        raise ValueError("benchmark CSV file exceeds the 5 MB limit")
    if not payload:
        raise ValueError("benchmark CSV file is empty")
    return payload, normalized_path


def refresh_market_benchmark(
    source_path=None,
    source_url=None,
    source_headers=None,
    progress_callback=None,
    timeout_seconds=15,
    opener=None,
):
    """Refresh exact MSCI ACWI levels from a local or authorized HTTPS CSV.

    This function intentionally has no public-site scraper, Yahoo ticker, ETF,
    or other proxy fallback. CSV input must contain canonical ``Date`` and
    ``Close`` columns and is validated atomically by :func:`upsert_close_series`.
    """
    if source_path is None and source_url is None:
        source_path = runtime_settings.get_setting("MSCI_ACWI_SOURCE_PATH", None)
        source_url = runtime_settings.get_setting("MSCI_ACWI_SOURCE_URL", None)
    if source_headers is None:
        source_headers = runtime_settings.get_setting("MSCI_ACWI_SOURCE_HEADERS", {})
    if source_headers is None:
        source_headers = {}

    if isinstance(source_path, str) and not source_path.strip():
        source_path = None
    if isinstance(source_url, str) and not source_url.strip():
        source_url = None
    if source_path is not None and source_url is not None:
        raise ValueError(
            "Configure only one MSCI ACWI benchmark source: "
            "MSCI_ACWI_SOURCE_PATH or MSCI_ACWI_SOURCE_URL"
        )
    if source_path is None and source_url is None:
        source_path = os.path.join(DATA_DIR, "imports", "msci_acwi.csv")

    if source_url is not None:
        source_kind = "authorized_https_csv"
        source_display = _safe_https_source_display(source_url)
    else:
        source_kind = "local_csv"
        source_display = _normalize_benchmark_source_path(source_path)
    _report_benchmark_refresh_progress(
        progress_callback, 1, "벤치마크 소스 확인"
    )

    if source_url is not None:
        payload, source_display = _read_limited_https_csv(
            source_url,
            source_headers,
            timeout_seconds,
            opener,
        )
    else:
        payload, source_display = _read_limited_local_csv(source_path)
    _report_benchmark_refresh_progress(
        progress_callback, 2, "벤치마크 CSV 읽기"
    )

    try:
        frame = pd.read_csv(io.BytesIO(payload))
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        raise ValueError("benchmark source is not a readable CSV") from exc
    summary = upsert_close_series(MARKET_BENCHMARK_TICKER, frame)
    _report_benchmark_refresh_progress(
        progress_callback, 3, "벤치마크 검증·저장"
    )

    conn = get_connection()
    try:
        cached_row = conn.execute(
            """
            SELECT COUNT(*), MIN(date), MAX(date)
            FROM ohlcv
            WHERE ticker = ? AND close IS NOT NULL
            """,
            (MARKET_BENCHMARK_TICKER,),
        ).fetchone()
    finally:
        conn.close()
    cached_rows, cached_first_date, cached_latest_date = cached_row
    if not cached_rows:
        raise RuntimeError("MSCI ACWI benchmark DB verification failed")

    summary.update(
        {
            "benchmark_name": MARKET_BENCHMARK_NAME,
            "source_kind": source_kind,
            "source_display": source_display,
            "cached_rows": int(cached_rows),
            "cached_first_date": cached_first_date,
            "cached_latest_date": cached_latest_date,
        }
    )
    _report_benchmark_refresh_progress(
        progress_callback, 4, "벤치마크 DB 확인"
    )
    return summary


def format_market_benchmark_refresh_summary(summary):
    source_name = {
        "local_csv": "Local CSV",
        "authorized_https_csv": "Authorized HTTPS CSV",
    }.get(summary.get("source_kind"), "Configured CSV")
    return (
        f"{MARKET_BENCHMARK_NAME} Benchmark Refresh Complete\n\n"
        f"Benchmark: {MARKET_BENCHMARK_NAME} ({MARKET_BENCHMARK_TICKER})\n"
        f"Source: {source_name} ({summary['source_display']})\n"
        f"Rows received/upserted: {summary['updated']}\n"
        f"Imported range: {summary['first_date']} to {summary['latest_date']}\n"
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
