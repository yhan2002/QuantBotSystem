import unittest
from unittest.mock import patch

import pandas as pd

import strategy_box_range
import core_entry_execution


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


def _short_reclaim_frame():
    df = _reclaim_frame().iloc[:20].copy()
    extra = pd.DataFrame(
        [
            {"Open": 113.0, "High": 116.0, "Low": 102.0, "Close": 113.0, "Volume": 1000.0},
            {"Open": 113.0, "High": 114.0, "Low": 108.0, "Close": 109.0, "Volume": 1000.0},
            {"Open": 108.0, "High": 109.0, "Low": 106.0, "Close": 107.0, "Volume": 1000.0},
            {"Open": 101.0, "High": 103.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0},
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0},
        ],
        index=pd.date_range(df.index[-1] + pd.offsets.BDay(), periods=5, freq="B"),
    )
    return pd.concat([df, extra])


class BoxRangeEngineTests(unittest.TestCase):
    def test_precomputed_entry_candidates_match_daily_signal_calculation(self):
        df = _reclaim_frame()
        candidates = strategy_box_range._precompute_entry_candidates(df, 20)

        for i in range(20, len(df)):
            signal, stats = strategy_box_range._entry_signal(df.iloc[i - 20:i], df.iloc[i])
            self.assertEqual(candidates["eligible"][i], signal == "LONG_RECLAIM")
            self.assertEqual(
                candidates["short_eligible"][i], signal == "SHORT_RECLAIM"
            )
            self.assertAlmostEqual(candidates["highest"][i], stats["highest"])
            self.assertAlmostEqual(candidates["lowest"][i], stats["lowest"])
            self.assertAlmostEqual(candidates["turnover_ratio"][i], stats["turnover_ratio"])

    @patch("strategy_box_range.core_universe_backtest.format_fixed_is_oos_backtest", return_value="\nSUMMARY")
    @patch("strategy_box_range.core_universe_backtest.aggregate_universe_backtest", return_value={})
    @patch("strategy_box_range._load_ohlcv")
    def test_universe_scan_loads_each_ticker_once(self, load_ohlcv, aggregate, _format):
        load_ohlcv.return_value = _reclaim_frame()

        strategy_box_range.scan_total_universe(["AAA", "BBB"])

        self.assertEqual(load_ohlcv.call_count, 2)
        self.assertEqual(aggregate.call_count, 2)

    @patch("strategy_box_range.core_universe_backtest.format_fixed_is_oos_backtest", return_value="\nSUMMARY")
    @patch("strategy_box_range.core_universe_backtest.aggregate_universe_backtest", return_value={})
    @patch("strategy_box_range._load_ohlcv")
    def test_short_only_mode_filters_both_box_backtest_windows(
        self, load_ohlcv, aggregate, _format
    ):
        load_ohlcv.return_value = _short_reclaim_frame()

        report = strategy_box_range.scan_total_universe(
            ["AAA"], position_mode="SHORT_ONLY"
        )

        self.assertIn("Position mode: Short Only", report)
        self.assertIn("SHORT Candidates", report)
        self.assertEqual(aggregate.call_count, 2)
        for call in aggregate.call_args_list:
            self.assertEqual(call.kwargs["side_filter"], "SHORT")

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
        self.assertEqual(details[0]["entry_price"], 102.0)
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

    def test_upper_false_break_reclaim_signals_short(self):
        df = _reclaim_frame().iloc[:21].copy()
        df.iloc[-1] = {"Open": 113.0, "High": 116.0, "Low": 102.0, "Close": 113.0, "Volume": 1000.0}
        result = strategy_box_range.analyze_box_range("TEST", df=df)
        self.assertEqual(result["status"], "SHORT_RECLAIM")
        self.assertEqual(result["upper_bounces"], 2)

    def test_short_center_then_opposite_edge_scales_out(self):
        avg_return, win_rate, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=_short_reclaim_frame(), side_filter="SHORT", include_trades=True
        )
        self.assertEqual(count, 1)
        self.assertEqual(win_rate, 100.0)
        self.assertAlmostEqual(avg_return, 8.19, places=2)
        self.assertEqual(details[0]["side"], "SHORT")
        self.assertEqual(len(details[0]["exit_events"]), 2)

    def test_short_side_filter_excludes_long_trades(self):
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

    def test_ohlc4_entry_does_not_exit_during_execution_day(self):
        _, _, count, _, details = strategy_box_range.run_box_backtest(
            "TEST",
            df=_reclaim_frame(),
            include_trades=True,
            entry_execution=core_entry_execution.NEXT_DAY_OHLC4,
        )
        self.assertEqual(count, 1)
        entry_date = details[0]["entry_date"]
        self.assertTrue(
            all(event["date"] > entry_date for event in details[0]["exit_events"])
        )


if __name__ == "__main__":
    unittest.main()
