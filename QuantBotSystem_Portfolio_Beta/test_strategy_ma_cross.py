import unittest
from unittest.mock import patch

import pandas as pd

import core_entry_execution
import strategy_ma_cross


class MomentumEvaluationMarkTests(unittest.TestCase):
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

        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(average, 5.0)
        self.assertEqual(details[0]["entry_date"], dates[-1].date().isoformat())
        self.assertEqual(
            details[0]["exit_events"][0]["reason"],
            "evaluation end mark",
        )
        self.assertEqual(
            details[0]["exit_date"],
            dates[-1].date().isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
