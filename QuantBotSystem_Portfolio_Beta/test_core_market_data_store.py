import unittest
import io
import os
import tempfile
from urllib.error import HTTPError
from unittest.mock import patch

import numpy as np
import pandas as pd

import test_experiment_momentum_volume_alpha_ab as subject
import core_market_data_store
import import_market_benchmark_csv


class MomentumVolumeAlphaTest(unittest.TestCase):
    def test_turnover_surge_uses_current_close_and_volume(self):
        dates = pd.date_range("2024-01-01", periods=8, freq="B")
        frame = pd.DataFrame(
            {
                "Open": np.arange(10.0, 18.0),
                "Close": np.arange(10.0, 18.0),
                "Volume": [100.0] * 7 + [1000.0],
            },
            index=dates,
        )

        result = subject.add_volume_alpha_columns(frame, lookback=5, turnover_multiple=3.0)

        self.assertFalse(bool(result["TURNOVER_SURGE"].iloc[-2]))
        self.assertTrue(bool(result["TURNOVER_SURGE"].iloc[-1]))
        self.assertEqual(result["POS_DAYS"].iloc[-1], 5.0)

    def test_combined_event_enters_at_next_open(self):
        dates = pd.date_range("2024-01-01", periods=12, freq="B")
        frame = pd.DataFrame(
            {
                "Open": [10.0] * 12,
                "Close": [10.0] * 12,
                "LONG_SIGNAL": [False] * 5 + [True] + [False] * 6,
                "SHORT_SIGNAL": [False] * 12,
                "LONG_SCORE": [1.0] * 12,
                "SHORT_SCORE": [-1.0] * 12,
                "TURNOVER_SURGE": [False] * 5 + [True] + [False] * 6,
                "ALPHA_NEUT": [1.0] * 12,
            },
            index=dates,
        )
        frame.loc[dates[6], "Open"] = 20.0
        frame.loc[dates[7], "Close"] = 22.0

        events = subject.build_combined_events(
            {"TEST": frame},
            windows=(2, 3, 4),
            holding_days=2,
            lookback=5,
            alpha_weight=0.5,
        )

        event = events[dates[5]]["LONG"][0]
        self.assertEqual(event["entry_idx"], 6)
        self.assertAlmostEqual(event["return"], 10.0)

    def test_backfill_with_empty_ticker_list_does_not_download(self):
        summary = core_market_data_store.backfill_price_data([])

        self.assertEqual(summary["requested"], 0)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["failed"], [])

    def test_listing_country_is_inferred_from_exchange_suffix(self):
        self.assertEqual(core_market_data_store._infer_listing_country("7203.T"), "JP")
        self.assertEqual(core_market_data_store._infer_listing_country("000001.SZ"), "CH")
        self.assertEqual(core_market_data_store._infer_listing_country("0700.HK"), "CH")
        self.assertEqual(core_market_data_store._infer_listing_country("005930.KS"), "KR")
        self.assertEqual(core_market_data_store._infer_listing_country("AAPL"), "US")

    def test_listing_country_refresh_uses_cached_ticker_symbols(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                summary = core_market_data_store.refresh_listing_country_labels_from_cache(
                    ["AAPL", "0700.HK", "7203.T", "005930.KS"]
                )
                metadata = core_market_data_store.get_security_metadata()

        self.assertEqual(summary["groups"], {"CH": 1, "JP": 1, "KR": 1, "US": 1})
        self.assertEqual(metadata["0700.HK"]["listing_country"], "CH")
        self.assertEqual(metadata["7203.T"]["listing_country"], "JP")

    def test_listing_country_refresh_excludes_reserved_benchmark_ticker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                core_market_data_store.upsert_close_series(
                    "MIWD00000PUS",
                    pd.DataFrame({"Date": ["2026-07-24"], "Close": [912.5]}),
                )
                summary = core_market_data_store.refresh_listing_country_labels_from_cache()
                metadata = core_market_data_store.get_security_metadata()

        self.assertEqual(summary["requested"], 0)
        self.assertNotIn("MIWD00000PUS", metadata)

    @patch("core_market_data_store._download_security_metadata")
    def test_refresh_security_metadata_persists_classification(self, download):
        download.return_value = {
            "listing_country": "US",
            "company_country": "United States",
            "sector": "Technology",
            "industry": "Semiconductors",
            "exchange": "NMS",
            "quote_type": "EQUITY",
            "short_name": "Example Inc.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                summary = core_market_data_store.refresh_security_metadata(
                    ["AAA"], sleep_seconds=0
                )
                metadata = core_market_data_store.get_security_metadata(["AAA"])["AAA"]

        self.assertEqual(summary["updated"], 1)
        self.assertEqual(metadata["sector"], "Technology")
        self.assertEqual(metadata["industry"], "Semiconductors")
        self.assertEqual(metadata["source"], "yfinance")

    @patch("core_market_data_store._reset_yfinance_cookie_session")
    @patch("core_market_data_store.yf.Ticker")
    def test_invalid_crumb_resets_session_and_retries_once(self, ticker_cls, reset):
        ticker_cls.return_value.get_info.side_effect = [
            RuntimeError("HTTP Error 401: Invalid Crumb"),
            {"sector": "Technology", "industry": "Software"},
        ]

        result = core_market_data_store._download_security_metadata("AAPL")

        reset.assert_called_once()
        self.assertEqual(ticker_cls.return_value.get_info.call_count, 2)
        self.assertEqual(result["sector"], "Technology")

    def test_latest_refresh_uses_cached_date_with_overlap(self):
        cached = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-20"])
        )
        downloaded = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-21", "2026-07-22", "2026-07-23"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "AAA", cached)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_history_range",
                    return_value=downloaded,
                ) as range_download, patch.object(
                    core_market_data_store, "_download_history"
                ) as period_download:
                    summary = core_market_data_store.refresh_latest_price_data(
                        ["AAA"],
                        sleep_seconds=0,
                        overlap_days=10,
                        as_of="2026-07-24",
                    )

                freshness = core_market_data_store.get_data_freshness(["AAA"])

        period_download.assert_not_called()
        range_download.assert_called_once()
        call = range_download.call_args
        self.assertEqual(call.args[0], "AAA")
        self.assertEqual(pd.Timestamp(call.kwargs["start"]), pd.Timestamp("2026-07-10"))
        self.assertEqual(pd.Timestamp(call.kwargs["end"]), pd.Timestamp("2026-07-25"))
        self.assertEqual(summary["incremental_downloads"], 1)
        self.assertEqual(summary["full_downloads"], 0)
        self.assertEqual(summary["updated"], 3)
        self.assertEqual(freshness, {"AAA": "2026-07-23"})

    def test_latest_refresh_bootstraps_uncached_ticker_with_full_period(self):
        downloaded = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-22", "2026-07-23"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store,
                "_download_history",
                return_value=downloaded,
            ) as period_download, patch.object(
                core_market_data_store, "_download_history_range"
            ) as range_download:
                summary = core_market_data_store.refresh_latest_price_data(
                    ["NEW"],
                    min_period="4y",
                    sleep_seconds=0,
                    as_of="2026-07-24",
                )

        period_download.assert_called_once_with("NEW", period="4y")
        range_download.assert_not_called()
        self.assertEqual(summary["full_downloads"], 1)
        self.assertEqual(summary["incremental_downloads"], 0)
        self.assertEqual(summary["updated"], 2)

    def test_data_freshness_reads_multiple_tickers(self):
        rows = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-22", "2026-07-23"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "AAA", rows)
                    core_market_data_store._upsert_ohlcv(conn, "BBB", rows.iloc[:1])
                finally:
                    conn.close()
                freshness = core_market_data_store.get_data_freshness(
                    ["BBB", "AAA", "AAA", "MISSING"]
                )

        self.assertEqual(
            freshness,
            {"AAA": "2026-07-23", "BBB": "2026-07-22"},
        )

    def test_close_series_insert_is_available_through_get_price_data(self):
        frame = pd.DataFrame(
            {
                "Date": ["2026-07-22", "2026-07-23"],
                "Close": [900.25, 904.75],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store.yf, "download"
            ) as yahoo_download:
                summary = core_market_data_store.upsert_close_series(
                    "MIWD00000PUS", frame
                )
                cached = core_market_data_store.get_price_data("MIWD00000PUS")

        yahoo_download.assert_not_called()
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["first_date"], "2026-07-22")
        self.assertEqual(summary["latest_date"], "2026-07-23")
        self.assertEqual(cached["Close"].tolist(), [900.25, 904.75])
        self.assertTrue(cached["Open"].isna().all())

    def test_close_series_reimport_updates_close_only(self):
        initial = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-23"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(
                        conn, "MIWD00000PUS", initial
                    )
                finally:
                    conn.close()

                core_market_data_store.upsert_close_series(
                    "MIWD00000PUS",
                    pd.DataFrame({"Date": ["2026-07-23"], "Close": [777.0]}),
                )
                cached = core_market_data_store.get_price_data("MIWD00000PUS")

        row = cached.iloc[0]
        self.assertEqual(row["Close"], 777.0)
        self.assertEqual(row["Open"], initial.iloc[0]["Open"])
        self.assertEqual(row["High"], initial.iloc[0]["High"])
        self.assertEqual(row["Low"], initial.iloc[0]["Low"])
        self.assertEqual(row["Adj Close"], initial.iloc[0]["Adj Close"])
        self.assertEqual(row["Volume"], initial.iloc[0]["Volume"])

    def test_invalid_close_series_has_no_partial_write(self):
        invalid_frames = {
            "bad_date": pd.DataFrame(
                {
                    "Date": ["2026-07-24", "not-a-date"],
                    "Close": [901.0, 902.0],
                }
            ),
            "duplicate_date": pd.DataFrame(
                {
                    "Date": ["2026-07-24", "2026-07-24 15:00:00"],
                    "Close": [901.0, 902.0],
                }
            ),
            "non_finite_close": pd.DataFrame(
                {
                    "Date": ["2026-07-24", "2026-07-25"],
                    "Close": [901.0, float("inf")],
                }
            ),
            "non_positive_close": pd.DataFrame(
                {
                    "Date": ["2026-07-24", "2026-07-25"],
                    "Close": [901.0, 0.0],
                }
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                core_market_data_store.upsert_close_series(
                    "MIWD00000PUS",
                    pd.DataFrame({"Date": ["2026-07-23"], "Close": [900.0]}),
                )
                for label, invalid_frame in invalid_frames.items():
                    with self.subTest(label=label):
                        with self.assertRaises(ValueError):
                            core_market_data_store.upsert_close_series(
                                "MIWD00000PUS", invalid_frame
                            )
                        cached = core_market_data_store.get_price_data(
                            "MIWD00000PUS"
                        )
                        self.assertEqual(len(cached), 1)
                        self.assertEqual(cached["Close"].iloc[0], 900.0)

    def test_local_csv_import_never_calls_yahoo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            csv_path = os.path.join(temp_dir, "acwi.csv")
            pd.DataFrame(
                {
                    "Date": ["2026-07-22", "2026-07-23"],
                    "Close": [900.25, 904.75],
                }
            ).to_csv(csv_path, index=False)
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store.yf, "download"
            ) as yahoo_download, patch.object(
                core_market_data_store.yf, "Ticker"
            ) as yahoo_ticker:
                summary = import_market_benchmark_csv.import_market_benchmark_csv(
                    csv_path
                )

        yahoo_download.assert_not_called()
        yahoo_ticker.assert_not_called()
        self.assertEqual(summary["ticker"], "MIWD00000PUS")
        self.assertEqual(summary["updated"], 2)

    def test_refresh_market_benchmark_uses_default_local_csv_and_reports_progress(self):
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            import_dir = os.path.join(temp_dir, "imports")
            os.makedirs(import_dir)
            csv_path = os.path.join(import_dir, "msci_acwi.csv")
            pd.DataFrame(
                {
                    "Date": ["2026-07-22", "2026-07-23"],
                    "Close": [900.25, 904.75],
                }
            ).to_csv(csv_path, index=False)
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store, "DATA_DIR", temp_dir
            ), patch.object(
                core_market_data_store.runtime_settings,
                "get_setting",
                return_value=None,
            ), patch.object(
                core_market_data_store.yf, "download"
            ) as yahoo_download, patch.object(
                core_market_data_store, "build_opener"
            ) as network_opener:
                summary = core_market_data_store.refresh_market_benchmark(
                    progress_callback=lambda completed, total, stage: progress.append(
                        (completed, total, stage)
                    )
                )
                cached = core_market_data_store.get_price_data("MIWD00000PUS")

        yahoo_download.assert_not_called()
        network_opener.assert_not_called()
        self.assertEqual(summary["ticker"], "MIWD00000PUS")
        self.assertEqual(summary["source_kind"], "local_csv")
        self.assertEqual(summary["source_display"], os.path.abspath(csv_path))
        self.assertEqual(summary["cached_rows"], 2)
        self.assertEqual(cached["Close"].tolist(), [900.25, 904.75])
        self.assertEqual(
            progress,
            [
                (1, 4, "벤치마크 소스 확인"),
                (2, 4, "벤치마크 CSV 읽기"),
                (3, 4, "벤치마크 검증·저장"),
                (4, 4, "벤치마크 DB 확인"),
            ],
        )
        formatted = core_market_data_store.format_market_benchmark_refresh_summary(
            summary
        )
        self.assertIn("MSCI ACWI (MIWD00000PUS)", formatted)
        self.assertIn("Rows received/upserted: 2", formatted)

    def test_refresh_market_benchmark_reads_authorized_https_without_leaking_details(self):
        payload = b"Date,Close\n2026-07-22,900.25\n2026-07-23,904.75\n"
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeHttpsResponse(
                payload,
                "https://licensed.example.com/private/final.csv?token=redirect-secret",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                summary = core_market_data_store.refresh_market_benchmark(
                    source_url=(
                        "https://licensed.example.com/private/acwi.csv"
                        "?token=request-secret"
                    ),
                    source_headers={"Authorization": "Bearer header-secret"},
                    timeout_seconds=7,
                    opener=opener,
                )

        self.assertEqual(summary["source_kind"], "authorized_https_csv")
        self.assertEqual(summary["source_display"], "https://licensed.example.com")
        self.assertNotIn("private", str(summary))
        self.assertNotIn("secret", str(summary))
        self.assertEqual(captured["timeout"], 7.0)
        self.assertEqual(
            captured["request"].get_header("Authorization"),
            "Bearer header-secret",
        )

    def test_refresh_market_benchmark_reads_url_and_headers_from_private_settings(self):
        payload = b"Date,Close\n2026-07-22,900.25\n"
        settings = {
            "MSCI_ACWI_SOURCE_PATH": None,
            "MSCI_ACWI_SOURCE_URL": "https://licensed.example.com/acwi.csv",
            "MSCI_ACWI_SOURCE_HEADERS": {
                "Authorization": "Bearer configured-secret"
            },
        }
        captured = {}

        def opener(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _FakeHttpsResponse(
                payload,
                "https://licensed.example.com/acwi.csv",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store.runtime_settings,
                "get_setting",
                side_effect=lambda name, default=None: settings.get(name, default),
            ):
                summary = core_market_data_store.refresh_market_benchmark(
                    opener=opener
                )

        self.assertEqual(summary["source_kind"], "authorized_https_csv")
        self.assertEqual(captured["authorization"], "Bearer configured-secret")

    def test_refresh_market_benchmark_missing_default_source_does_not_touch_db_or_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store, "DATA_DIR", temp_dir
            ), patch.object(
                core_market_data_store.runtime_settings,
                "get_setting",
                return_value=None,
            ), patch.object(
                core_market_data_store, "build_opener"
            ) as network_opener:
                with self.assertRaisesRegex(FileNotFoundError, "Date/Close CSV"):
                    core_market_data_store.refresh_market_benchmark()

            network_opener.assert_not_called()
            self.assertFalse(os.path.exists(db_path))

    def test_refresh_market_benchmark_rejects_non_https_without_opening_it(self):
        opener_calls = []

        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            core_market_data_store.refresh_market_benchmark(
                source_url="http://example.com/acwi.csv",
                opener=lambda *args, **kwargs: opener_calls.append((args, kwargs)),
            )

        self.assertEqual(opener_calls, [])

    def test_refresh_market_benchmark_rejects_oversized_https_response(self):
        response = _FakeHttpsResponse(
            b"",
            "https://licensed.example.com/acwi.csv",
            content_length=core_market_data_store.MARKET_BENCHMARK_MAX_CSV_BYTES + 1,
        )

        with self.assertRaisesRegex(ValueError, "exceeds the 5 MB limit"):
            core_market_data_store.refresh_market_benchmark(
                source_url="https://licensed.example.com/acwi.csv",
                opener=lambda request, timeout: response,
            )

        self.assertFalse(response.read_called)

    def test_refresh_market_benchmark_rejects_cross_origin_redirect(self):
        response = _FakeHttpsResponse(
            b"Date,Close\n2026-07-22,900.25\n",
            "https://different.example.com/acwi.csv",
        )

        with self.assertRaisesRegex(ValueError, "different host"):
            core_market_data_store.refresh_market_benchmark(
                source_url="https://licensed.example.com/acwi.csv",
                opener=lambda request, timeout: response,
            )

    def test_production_redirect_handler_blocks_cross_origin_before_request_creation(self):
        handler = core_market_data_store._SameOriginHttpsRedirectHandler(
            ("licensed.example.com", 443)
        )
        request = core_market_data_store.Request(
            "https://licensed.example.com/acwi.csv",
            headers={"Authorization": "Bearer header-secret"},
        )

        with self.assertRaisesRegex(ValueError, "different host"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://different.example.com/acwi.csv",
            )

    def test_refresh_market_benchmark_redacts_url_on_authorization_error(self):
        secret_url = "https://licensed.example.com/acwi.csv?token=secret-value"

        def opener(request, timeout):
            raise HTTPError(secret_url, 401, "Unauthorized", {}, None)

        with self.assertRaises(RuntimeError) as captured:
            core_market_data_store.refresh_market_benchmark(
                source_url=secret_url,
                source_headers={"Authorization": "Bearer header-secret"},
                opener=opener,
            )

        message = str(captured.exception)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("header-secret", message)

    def test_refresh_market_benchmark_rejects_invalid_header_without_echoing_secret(self):
        with self.assertRaises(ValueError) as captured:
            core_market_data_store.refresh_market_benchmark(
                source_url="https://licensed.example.com/acwi.csv",
                source_headers={
                    "Authorization": "Bearer header-secret\r\nX-Injected: yes"
                },
                opener=lambda request, timeout: self.fail(
                    "invalid headers must be rejected before opening a URL"
                ),
            )

        self.assertIn("invalid header", str(captured.exception))
        self.assertNotIn("header-secret", str(captured.exception))


def _ohlcv_frame(dates):
    index = pd.to_datetime(dates)
    values = np.arange(len(index), dtype=float) + 100.0
    return pd.DataFrame(
        {
            "Open": values,
            "High": values + 1.0,
            "Low": values - 1.0,
            "Close": values + 0.5,
            "Adj Close": values + 0.5,
            "Volume": np.full(len(index), 1000.0),
        },
        index=index,
    )


class _FakeHttpsResponse:
    def __init__(self, payload, final_url, content_length=None):
        self._payload = payload
        self._final_url = final_url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_called = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self._final_url

    def read(self, size=-1):
        self.read_called = True
        return io.BytesIO(self._payload).read(size)


if __name__ == "__main__":
    unittest.main()
