import unittest

import numpy as np
import pandas as pd

import run_momentum_volume_alpha_ab as subject
import market_data_store


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
        summary = market_data_store.backfill_price_data([])

        self.assertEqual(summary["requested"], 0)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["failed"], [])


if __name__ == "__main__":
    unittest.main()
