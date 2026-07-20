import unittest

import pandas as pd

import core_entry_execution


class EntryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.prices = pd.DataFrame(
            [
                {"Open": 99.0, "High": 101.0, "Low": 98.0, "Close": 100.0},
                {"Open": 103.0, "High": 106.0, "Low": 102.0, "Close": 105.0},
            ]
        )

    def test_next_open_uses_next_session_open(self):
        result = core_entry_execution.resolve_entry(self.prices, 0, "LONG")
        self.assertEqual(result["price"], 103.0)
        self.assertTrue(result["allow_entry_day_exit"])

    def test_next_day_ohlc4_is_end_of_session_proxy(self):
        result = core_entry_execution.resolve_entry(
            self.prices, 0, "LONG", mode=core_entry_execution.NEXT_DAY_OHLC4
        )
        self.assertEqual(result["price"], 104.0)
        self.assertFalse(result["allow_entry_day_exit"])

    def test_adverse_gap_filter_rejects_directional_chasing(self):
        long_result = core_entry_execution.resolve_entry(
            self.prices,
            0,
            "LONG",
            mode=core_entry_execution.ADVERSE_GAP_FILTER,
            adverse_gap_limit_pct=2.0,
        )
        self.assertIsNone(long_result)

        falling = self.prices.copy()
        falling.loc[1, "Open"] = 97.0
        short_result = core_entry_execution.resolve_entry(
            falling,
            0,
            "SHORT",
            mode=core_entry_execution.ADVERSE_GAP_FILTER,
            adverse_gap_limit_pct=2.0,
        )
        self.assertIsNone(short_result)


if __name__ == "__main__":
    unittest.main()
