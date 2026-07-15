import unittest

import pandas as pd

import strategy_box_range


def _reclaim_frame():
    rows = []
    for i in range(20):
        rows.append(
            {
                "Open": 107.0,
                "High": 115.0 if i in (5, 12) else 112.0,
                "Low": 100.0 if i in (1, 8) else 103.0,
                "Close": 103.0 if i in (1, 8) else 107.0,
                "Volume": 1000.0,
            }
        )
    rows.extend(
        [
            {"Open": 102.0, "High": 112.0, "Low": 99.0, "Close": 102.0, "Volume": 1000.0},
            {"Open": 102.0, "High": 108.0, "Low": 101.0, "Close": 107.0, "Volume": 1000.0},
            {"Open": 110.0, "High": 116.0, "Low": 109.0, "Close": 115.0, "Volume": 1000.0},
            {"Open": 115.0, "High": 115.0, "Low": 114.0, "Close": 114.0, "Volume": 1000.0},
        ]
    )
    return pd.DataFrame(rows, index=pd.date_range("2025-01-01", periods=len(rows), freq="B"))


class BoxRangeEngineTests(unittest.TestCase):
    def test_lower_false_break_reclaim_signals_next_open_long(self):
        result = strategy_box_range.analyze_box_range("TEST", df=_reclaim_frame().iloc[:21])
        self.assertEqual(result["status"], "LONG_RECLAIM")
        self.assertEqual(result["lower_bounces"], 2)

    def test_center_then_opposite_edge_scales_out(self):
        avg_return, win_rate, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=_reclaim_frame(), side_filter="LONG", include_trades=True
        )
        self.assertEqual(count, 1)
        self.assertEqual(win_rate, 100.0)
        self.assertAlmostEqual(avg_return, 9.07, places=2)
        self.assertEqual(details[0]["entry_date"], "2025-01-30")
        self.assertEqual(len(details[0]["exit_events"]), 2)

    def test_next_open_must_be_between_lower_boundary_and_midpoint(self):
        for invalid_open in (99.0, 100.0, 107.5, 108.0):
            with self.subTest(invalid_open=invalid_open):
                df = _reclaim_frame().copy()
                df.iloc[21, df.columns.get_loc("Open")] = invalid_open
                _, _, count, _, details = strategy_box_range.run_box_backtest(
                    "TEST", df=df, include_trades=True
                )
                self.assertEqual(count, 0)
                self.assertEqual(details, [])

    def test_remaining_position_exits_ten_days_after_center(self):
        rows = _reclaim_frame().iloc[:22].to_dict("records")
        rows.extend(
            {"Open": 106.0, "High": 110.0, "Low": 104.0, "Close": 106.0, "Volume": 1000.0}
            for _ in range(10)
        )
        df = pd.DataFrame(rows, index=pd.date_range("2025-01-01", periods=len(rows), freq="B"))
        _, _, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=df, include_trades=True
        )
        self.assertEqual(count, 1)
        self.assertEqual(details[0]["exit_events"][-1]["reason"], "10d post-center exit")
        self.assertEqual(len(details[0]["exit_events"]), 2)

    def test_upper_false_break_reclaim_is_not_a_signal(self):
        df = _reclaim_frame().iloc[:21].copy()
        df.iloc[-1] = {"Open": 113.0, "High": 116.0, "Low": 102.0, "Close": 113.0, "Volume": 1000.0}
        result = strategy_box_range.analyze_box_range("TEST", df=df)
        self.assertEqual(result["status"], "BOX_QUALIFIED")
        self.assertEqual(result["upper_bounces"], 2)

    def test_short_side_filter_cannot_create_trades(self):
        _, _, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=_reclaim_frame(), side_filter="SHORT", include_trades=True
        )
        self.assertEqual(count, 0)
        self.assertEqual(details, [])

    def test_turnover_surge_blocks_entry(self):
        df = _reclaim_frame().iloc[:21].copy()
        df.iloc[-1, df.columns.get_loc("Volume")] = 3000.0
        result = strategy_box_range.analyze_box_range("TEST", df=df)
        self.assertEqual(result["status"], "VOLUME_VETO")


if __name__ == "__main__":
    unittest.main()
