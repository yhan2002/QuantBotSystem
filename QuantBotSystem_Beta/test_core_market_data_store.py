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


if __name__ == "__main__":
    unittest.main()
