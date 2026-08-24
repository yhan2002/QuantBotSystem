import unittest

import pandas as pd

import strategy_box_range_new as strategy


def _history():
    dates = pd.date_range("2025-01-01", periods=21, freq="B")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 101.0,
            "Low": 99.0,
            "Close": 100.0,
            "Volume": 100.0,
        },
        index=dates,
    )


def _append(frame, **values):
    row = {
        "Open": 102.0,
        "High": 102.5,
        "Low": 101.2,
        "Close": 102.0,
        "Volume": 100.0,
    }
    row.update(values)
    date = frame.index[-1] + pd.offsets.BDay()
    return pd.concat([frame, pd.DataFrame([row], index=[date])])


def _long_signal_frame():
    return _append(
        _history(),
        Open=100.0,
        High=102.2,
        Low=99.8,
        Close=102.0,
        Volume=200.0,
    )


def _short_signal_frame():
    return _append(
        _history(),
        Open=100.0,
        High=100.2,
        Low=97.8,
        Close=98.0,
        Volume=210.0,
    )


class BoxRangeNewTests(unittest.TestCase):
    def test_long_short_score_and_prior_bar_causality(self):
        long_frame = _long_signal_frame()
        long_result = strategy.analyze_box_range_new("TEST", df=long_frame)
        short_result = strategy.analyze_box_range_new(
            "TEST", df=_short_signal_frame()
        )

        self.assertEqual(long_result["status"], "LONG_BREAKOUT")
        self.assertEqual(short_result["status"], "SHORT_BREAKOUT")
        self.assertAlmostEqual(long_result["upper"], 101.0)
        self.assertAlmostEqual(long_result["lower"], 99.0)
        self.assertAlmostEqual(long_result["width_atr"], 1.0)
        self.assertAlmostEqual(long_result["box_width_pct"], 0.02)
        self.assertAlmostEqual(long_result["raw_score"], 7.0 / 12.0)
        self.assertAlmostEqual(short_result["raw_score"], -7.0 / 12.0)

        before = strategy._signal_stats(long_frame, 21)
        extended = _append(
            _append(long_frame, Open=999, High=1200, Low=1, Close=900),
            Open=1,
            High=2,
            Low=0.1,
            Close=1,
        )
        after = strategy._signal_stats(extended, 21)
        self.assertEqual(before, after)

    def test_next_open_gate_is_directional_and_bounded(self):
        long_stats = strategy.analyze_box_range_new("TEST", df=_long_signal_frame())
        short_stats = strategy.analyze_box_range_new("TEST", df=_short_signal_frame())
        self.assertTrue(strategy._valid_next_open(102.0, long_stats, "LONG"))
        self.assertFalse(strategy._valid_next_open(101.0, long_stats, "LONG"))
        self.assertFalse(strategy._valid_next_open(102.01, long_stats, "LONG"))
        self.assertTrue(strategy._valid_next_open(98.0, short_stats, "SHORT"))
        self.assertFalse(strategy._valid_next_open(99.0, short_stats, "SHORT"))
        self.assertFalse(strategy._valid_next_open(97.99, short_stats, "SHORT"))

        rejected = _append(
            _long_signal_frame(),
            Open=102.01,
            High=102.5,
            Low=101.5,
            Close=102.0,
        )
        self.assertEqual(strategy.run_box_backtest("TEST", df=rejected)[2], 0)

    def test_long_and_short_targets_exit_in_full(self):
        long_frame = _append(
            _long_signal_frame(),
            Open=101.5,
            High=103.2,
            Low=101.2,
            Close=102.0,
        )
        short_frame = _append(
            _short_signal_frame(),
            Open=98.5,
            High=98.8,
            Low=96.8,
            Close=98.0,
        )
        long_trade = strategy.run_box_backtest(
            "TEST", df=long_frame, include_trades=True
        )[4][0]
        short_trade = strategy.run_box_backtest(
            "TEST", df=short_frame, include_trades=True
        )[4][0]

        self.assertEqual(long_trade["exit_reason"], "box target")
        self.assertEqual(short_trade["exit_reason"], "box target")
        self.assertAlmostEqual(long_trade["exit_price"], 103.0)
        self.assertAlmostEqual(short_trade["exit_price"], 97.0)
        self.assertEqual(long_trade["exit_events"][0]["weight"], 1.0)

    def test_failure_exits_next_open_but_target_has_intraday_priority(self):
        failed = _append(
            _long_signal_frame(),
            Open=101.5,
            High=102.5,
            Low=100.5,
            Close=100.8,
        )
        failed = _append(
            failed,
            Open=100.4,
            High=101.0,
            Low=100.0,
            Close=100.5,
        )
        trade = strategy.run_box_backtest(
            "TEST", df=failed, include_trades=True
        )[4][0]
        self.assertEqual(trade["exit_reason"], "box failure next open")
        self.assertAlmostEqual(trade["exit_price"], 100.4)

        target_first = failed.copy()
        target_first.iloc[22, target_first.columns.get_loc("High")] = 103.2
        trade = strategy.run_box_backtest(
            "TEST", df=target_first, include_trades=True
        )[4][0]
        self.assertEqual(trade["exit_reason"], "box target")

    def test_tenth_session_fixed_exit_and_evaluation_mark(self):
        frame = _long_signal_frame()
        for _ in range(10):
            frame = _append(
                frame,
                Open=101.5,
                High=102.7,
                Low=101.2,
                Close=102.0,
            )
        native = strategy.run_box_backtest(
            "TEST", df=frame, include_trades=True
        )[4][0]
        self.assertEqual(native["exit_reason"], "10-session close")
        self.assertEqual(
            native["exit_date"], frame.index[31].date().isoformat()
        )

        short_frame = frame.iloc[:25]
        marked = strategy.run_box_backtest(
            "TEST", df=short_frame, include_trades=True
        )[4][0]
        self.assertEqual(marked["exit_reason"], "evaluation end mark")

        fixed = strategy.run_box_backtest(
            "TEST", df=frame, include_trades=True, fixed_exit_sessions=2
        )[4][0]
        self.assertEqual(fixed["exit_reason"], "D+2 fixed close")
        self.assertEqual(fixed["exit_date"], frame.index[24].date().isoformat())

        excluded = strategy.run_box_backtest(
            "TEST", df=frame, side_filter="SHORT", include_trades=True
        )
        self.assertEqual(excluded[2], 0)


if __name__ == "__main__":
    unittest.main()
