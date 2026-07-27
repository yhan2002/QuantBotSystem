import unittest
from unittest.mock import patch

import pandas as pd

import core_entry_execution
import strategy_ma_cross


class MomentumEvaluationMarkTests(unittest.TestCase):
    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "aggregate_universe_backtest",
    )
    @patch.object(strategy_ma_cross, "analyze_trend_strict_signals")
    def test_scan_reports_current_is_and_oos_progress_safely(
        self,
        analyze,
        aggregate,
        _format,
    ):
        analyze.side_effect = [
            ("EXIT_SIGNAL", 1.0),
            ("ERROR", 0.0),
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

        report = strategy_ma_cross.scan_ma_cross(
            ["AAA", "BBB"],
            progress_callback=progress,
        )

        self.assertIn("Momentum Scan Report", report)
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

    def test_incomplete_holding_period_is_marked_at_evaluation_close(self):
        dates = pd.date_range("2024-01-01", periods=275, freq="B")
        frame = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1000.0,
            },
            index=dates,
        )
        frame.iloc[-1, frame.columns.get_loc("Close")] = 105.0
        future = frame.iloc[[-1]].copy()
        future.index = [dates[-1] + pd.offsets.BDay(1)]
        future.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        extended = pd.concat([frame, future])

        def add_signals(prices, **_kwargs):
            result = prices.copy()
            result["LONG_SIGNAL"] = False
            result["SHORT_SIGNAL"] = False
            result["SIGNAL_STRENGTH"] = 0.0
            result.iloc[-2, result.columns.get_loc("LONG_SIGNAL")] = True
            result.iloc[-2, result.columns.get_loc("SIGNAL_STRENGTH")] = 2.0
            return result

        def next_open(prices, signal_index, side, **_kwargs):
            self.assertEqual(side, "LONG")
            entry_index = signal_index + 1
            return {
                "price": float(prices["Open"].iloc[entry_index]),
                "mode": core_entry_execution.NEXT_OPEN,
                "signal_close": float(prices["Close"].iloc[signal_index]),
                "next_open": float(prices["Open"].iloc[entry_index]),
                "signal_to_open_return_pct": 0.0,
                "signal_to_fill_return_pct": 0.0,
            }

        with patch.object(
            strategy_ma_cross,
            "_add_ma_signal_columns",
            side_effect=add_signals,
        ), patch.object(
            strategy_ma_cross.core_entry_execution,
            "resolve_entry",
            side_effect=next_open,
        ):
            average, win_rate, count, _, details = (
                strategy_ma_cross.run_isolated_backtest(
                    "TEST",
                    df=frame,
                    include_trades=True,
                )
            )
            cutoff_result = strategy_ma_cross.run_isolated_backtest(
                "TEST",
                df=extended,
                include_trades=True,
                evaluation_end=dates[-1],
            )

        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(average, 5.0)
        self.assertEqual(
            details[0]["signal_date"], dates[-2].date().isoformat()
        )
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(details[0]["entry_date"], dates[-1].date().isoformat())
        self.assertEqual(
            details[0]["exit_events"][0]["reason"],
            "evaluation end mark",
        )
        self.assertEqual(
            details[0]["exit_date"],
            dates[-1].date().isoformat(),
        )
        self.assertEqual(cutoff_result[4], details)


if __name__ == "__main__":
    unittest.main()
