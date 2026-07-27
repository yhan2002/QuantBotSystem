import unittest
import os
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd

import test_experiment_momentum_volume_alpha_ab as subject
import core_market_data_store


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
        rows = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-24"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", rows)
                    core_market_data_store._upsert_ohlcv(
                        conn, "MIWD00000PUS", rows
                    )
                finally:
                    conn.close()
                summary = core_market_data_store.refresh_listing_country_labels_from_cache()
                metadata = core_market_data_store.get_security_metadata()

        self.assertEqual(summary["requested"], 0)
        self.assertNotIn("ACWI", metadata)
        self.assertNotIn("MIWD00000PUS", metadata)

    @patch("core_market_data_store._download_security_metadata")
    def test_metadata_refresh_excludes_reserved_benchmark_tickers(self, download):
        download.return_value = {"sector": "Technology"}
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                summary = core_market_data_store.refresh_security_metadata(
                    ["ACWI", "MIWD00000PUS", "AAA"],
                    sleep_seconds=0,
                )

        self.assertEqual(summary["requested"], 1)
        download.assert_called_once_with("AAA")

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

    def test_refresh_market_benchmark_downloads_full_acwi_history_and_reports_progress(self):
        progress = []
        downloaded = _valid_benchmark_frame()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store,
                "_download_market_benchmark_history",
                return_value=downloaded,
            ) as range_download:
                summary = core_market_data_store.refresh_market_benchmark(
                    progress_callback=lambda completed, total, stage: progress.append(
                        (completed, total, stage)
                    ),
                    as_of="2026-07-27",
                )
                cached = core_market_data_store.get_price_data("ACWI")

        range_download.assert_called_once_with(
            "ACWI",
            start="2023-12-01",
            end="2026-07-28",
        )
        self.assertEqual(summary["ticker"], "ACWI")
        self.assertEqual(summary["source_kind"], "yfinance")
        self.assertEqual(summary["data_basis"], "Adj Close")
        self.assertEqual(summary["cached_rows"], len(downloaded))
        self.assertEqual(cached["Adj Close"].tolist(), downloaded["Adj Close"].tolist())
        self.assertEqual(
            progress,
            [
                (1, 4, "ACWI 다운로드 준비"),
                (2, 4, "ACWI 가격 다운로드"),
                (3, 4, "ACWI Adj Close 검증"),
                (4, 4, "ACWI DB 확인 완료"),
            ],
        )
        formatted = core_market_data_store.format_market_benchmark_refresh_summary(
            summary
        )
        self.assertIn("Benchmark: ACWI", formatted)
        self.assertIn("Data basis: Adj Close", formatted)
        self.assertIn(f"Rows received/upserted: {len(downloaded)}", formatted)

    def test_refresh_market_benchmark_restates_older_adjusted_closes(self):
        initial = core_market_data_store._normalize_download(
            _ohlcv_frame(
                [
                    pd.Timestamp("2023-11-30"),
                    *pd.bdate_range("2023-12-29", "2026-07-24"),
                ]
            )
        )
        refreshed = _valid_benchmark_frame()
        restated_date = pd.Timestamp("2024-01-02").date()
        refreshed.loc[restated_date, "Adj Close"] = 95.0
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", initial)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_market_benchmark_history",
                    return_value=refreshed,
                ) as full_download:
                    core_market_data_store.refresh_market_benchmark(
                        as_of="2026-07-27"
                    )
                cached = core_market_data_store.get_price_data("ACWI")

        full_download.assert_called_once()
        self.assertNotIn(pd.Timestamp("2023-11-30"), cached.index)
        self.assertEqual(cached.loc["2024-01-02", "Adj Close"], 95.0)

    def test_refresh_market_benchmark_raises_clear_error_on_download_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path), patch.object(
                core_market_data_store,
                "_download_market_benchmark_history",
                side_effect=RuntimeError("offline"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Yahoo returned no usable OHLCV data"
                ):
                    core_market_data_store.refresh_market_benchmark()

    def test_invalid_downloaded_adj_close_does_not_modify_existing_cache(self):
        initial = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-22"])
        )
        invalid = _valid_benchmark_frame()
        invalid.loc[invalid.index[0], "Adj Close"] = float("nan")
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", initial)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_market_benchmark_history",
                    return_value=invalid,
                ):
                    with self.assertRaisesRegex(RuntimeError, "finite and positive"):
                        core_market_data_store.refresh_market_benchmark(
                            as_of="2026-07-27"
                        )
                cached = core_market_data_store.get_price_data("ACWI")

        self.assertEqual(len(cached), 1)
        self.assertEqual(cached.index[0], pd.Timestamp("2026-07-22"))
        self.assertEqual(cached["Adj Close"].iloc[0], 100.5)

    def test_partial_benchmark_download_does_not_replace_existing_cache(self):
        initial = _valid_benchmark_frame()
        partial = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-23", "2026-07-24"])
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", initial)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_market_benchmark_history",
                    return_value=partial,
                ):
                    with self.assertRaisesRegex(RuntimeError, "no pre-IS seed"):
                        core_market_data_store.refresh_market_benchmark(
                            as_of="2026-07-27"
                        )
                cached = core_market_data_store.get_price_data("ACWI")

        self.assertEqual(len(cached), len(initial))
        self.assertEqual(cached.index[0], pd.Timestamp("2023-12-29"))

    def test_stale_benchmark_download_is_rejected(self):
        stale = core_market_data_store._normalize_download(
            _ohlcv_frame(pd.bdate_range("2023-12-29", "2026-06-30"))
        )
        with patch.object(
            core_market_data_store,
            "_download_market_benchmark_history",
            return_value=stale,
        ):
            with self.assertRaisesRegex(RuntimeError, "calendar days stale"):
                core_market_data_store.refresh_market_benchmark(
                    as_of="2026-07-27"
                )

    def test_stale_pre_is_seed_is_rejected(self):
        dates = [
            pd.Timestamp("2023-12-01"),
            *pd.bdate_range("2024-01-02", "2026-07-24"),
        ]
        stale_seed = core_market_data_store._normalize_download(
            _ohlcv_frame(dates)
        )
        with patch.object(
            core_market_data_store,
            "_download_market_benchmark_history",
            return_value=stale_seed,
        ):
            with self.assertRaisesRegex(RuntimeError, "pre-IS seed"):
                core_market_data_store.refresh_market_benchmark(
                    as_of="2026-07-27"
                )

    def test_sparse_benchmark_download_is_rejected(self):
        dates = pd.bdate_range("2023-12-29", "2026-07-24")
        sparse = core_market_data_store._normalize_download(
            _ohlcv_frame(dates[::2])
        )
        with patch.object(
            core_market_data_store,
            "_download_market_benchmark_history",
            return_value=sparse,
        ):
            with self.assertRaisesRegex(RuntimeError, "appears partial"):
                core_market_data_store.refresh_market_benchmark(
                    as_of="2026-07-27"
                )

    def test_missing_native_adjusted_close_does_not_replace_existing_cache(self):
        initial = _valid_benchmark_frame()
        missing_adjusted = _ohlcv_frame(
            pd.bdate_range("2023-12-29", "2026-07-24")
        ).drop(columns=["Adj Close"])
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", initial)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_market_benchmark_history",
                    return_value=missing_adjusted,
                ):
                    with self.assertRaisesRegex(RuntimeError, "missing Adj Close"):
                        core_market_data_store.refresh_market_benchmark(
                            as_of="2026-07-27"
                        )
                cached = core_market_data_store.get_price_data("ACWI")

        self.assertEqual(len(cached), len(initial))

    def test_benchmark_database_write_failure_rolls_back_full_replace(self):
        initial = core_market_data_store._normalize_download(
            _ohlcv_frame(["2026-07-22"])
        )
        downloaded = _valid_benchmark_frame()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "market_data.sqlite")
            with patch.object(core_market_data_store, "DB_PATH", db_path):
                conn = core_market_data_store.get_connection()
                try:
                    core_market_data_store._upsert_ohlcv(conn, "ACWI", initial)
                finally:
                    conn.close()
                with patch.object(
                    core_market_data_store,
                    "_download_market_benchmark_history",
                    return_value=downloaded,
                ), patch.object(
                    core_market_data_store,
                    "_upsert_ohlcv",
                    side_effect=RuntimeError("write failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "write failed"):
                        core_market_data_store.refresh_market_benchmark(
                            as_of="2026-07-27"
                        )
                cached = core_market_data_store.get_price_data("ACWI")

        self.assertEqual(len(cached), 1)
        self.assertEqual(cached.index[0], pd.Timestamp("2026-07-22"))


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


def _valid_benchmark_frame():
    return core_market_data_store._normalize_download(
        _ohlcv_frame(pd.bdate_range("2023-12-29", "2026-07-24"))
    )


if __name__ == "__main__":
    unittest.main()
