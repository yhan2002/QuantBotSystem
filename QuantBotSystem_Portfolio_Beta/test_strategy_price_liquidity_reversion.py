import unittest
from unittest.mock import patch

import pandas as pd

import strategy_price_liquidity_reversion


def _long_reversion_frame():
    close = [100 + 0.2 * index for index in range(74)]
    close[65:71] = [100.0, 102.0, 103.0, 109.0, 114.0, 115.0]
    close[71:] = [115.0] * (len(close) - 71)
    rows = []
    for index, current in enumerate(close):
        previous = close[index - 1] if index else current
        rows.append(
            {
                "Open": previous,
                "High": max(current, previous) + 1.0,
                "Low": min(current, previous) - 1.0,
                "Close": current,
                "Volume": 1000.0,
            }
        )
    return pd.DataFrame(
        rows, index=pd.date_range("2025-01-01", periods=len(rows), freq="B")
    )


class ValueReversionEngineTests(unittest.TestCase):
    @patch.object(
        strategy_price_liquidity_reversion.core_universe_backtest,
        "format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch.object(
        strategy_price_liquidity_reversion.core_universe_backtest,
        "aggregate_universe_backtest",
    )
    @patch.object(
        strategy_price_liquidity_reversion,
        "analyze_value_reversion",
    )
    def test_scan_reports_current_is_and_oos_progress_safely(
        self,
        analyze,
        aggregate,
        _format,
    ):
        analyze.side_effect = [
            {"status": "NO_SIGNAL"},
            {"status": "DATA_INSUFFICIENT"},
        ]
        events = []

        def progress(stage, completed, total, ticker, succeeded):
            events.append((stage, completed, total, ticker, succeeded))
            raise RuntimeError("Telegram edit failed")

        def aggregate_result(*_args, **kwargs):
            callback = kwargs["progress_callback"]
            callback(1, 2, "AAA", True)
            callback(2, 2, "BBB", False)
            return {}

        aggregate.side_effect = aggregate_result

        report = strategy_price_liquidity_reversion.scan_value_reversion(
            ["AAA", "BBB"],
            progress_callback=progress,
        )

        self.assertIn("Reversion Scan Report", report)
        self.assertEqual(
            events,
            [
                ("CURRENT_SCAN", 1, 2, "AAA", True),
                ("CURRENT_SCAN", 2, 2, "BBB", False),
                ("IS_BACKTEST", 1, 2, "AAA", True),
                ("IS_BACKTEST", 2, 2, "BBB", False),
                ("OOS_BACKTEST", 1, 2, "AAA", True),
                ("OOS_BACKTEST", 2, 2, "BBB", False),
            ],
        )

    def test_confirmed_shock_generates_next_open_long_signal(self):
        result = strategy_price_liquidity_reversion.analyze_value_reversion(
            "TEST", df=_long_reversion_frame().iloc[:67]
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["side"], "LONG")

    def test_long_signal_requires_positive_rebound_candle(self):
        frame = _long_reversion_frame().iloc[:67].copy()
        frame.iloc[-1, frame.columns.get_loc("Open")] = frame["Close"].iloc[-1] + 1.0

        result = strategy_price_liquidity_reversion.analyze_value_reversion("TEST", df=frame)

        self.assertEqual(result["status"], "NO_SIGNAL")

    def test_partial_exit_moves_stop_to_entry_price(self):
        self.assertEqual(
            strategy_price_liquidity_reversion._post_partial_stop_price("LONG", 100.0, 95.0),
            100.0,
        )
        self.assertEqual(
            strategy_price_liquidity_reversion._post_partial_stop_price("SHORT", 100.0, 105.0),
            100.0,
        )

    def test_b_exit_scales_at_half_sigma_then_mean(self):
        frame = _long_reversion_frame()
        avg_return, win_rate, count, _, details = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST", "LONG", df=frame, include_trades=True
            )
        )
        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(avg_return, 9.31, places=2)
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(details[0]["entry_date"], "2025-04-04")
        self.assertEqual(details[0]["entry_price"], 102.0)
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertEqual(details[0]["exit_events"][1]["reason"], "mean reversion")

        cutoff = frame.index[-1]
        future = frame.iloc[[-1]].copy()
        future.index = [cutoff + pd.offsets.BDay(1)]
        future.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        extended = pd.concat([frame, future])
        cutoff_result = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST",
                "LONG",
                df=extended,
                include_trades=True,
                evaluation_end=cutoff,
            )
        )
        self.assertEqual(cutoff_result[4], details)

    def test_open_position_is_marked_at_evaluation_close(self):
        frame = _long_reversion_frame().iloc[:68]

        _, _, count, _, details = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST",
                "LONG",
                df=frame,
                include_trades=True,
            )
        )

        self.assertEqual(count, 1)
        self.assertEqual(
            details[0]["exit_events"][-1]["reason"],
            "evaluation end mark",
        )
        self.assertEqual(
            details[0]["exit_events"][-1]["date"],
            frame.index[-1].date().isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
