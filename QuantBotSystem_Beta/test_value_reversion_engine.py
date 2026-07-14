import unittest

import pandas as pd

import value_reversion_engine


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
    def test_confirmed_shock_generates_next_open_long_signal(self):
        result = value_reversion_engine.analyze_value_reversion(
            "TEST", df=_long_reversion_frame().iloc[:67]
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["side"], "LONG")

    def test_b_exit_scales_at_half_sigma_then_mean(self):
        avg_return, win_rate, count, _, details = (
            value_reversion_engine.run_reversion_backtest(
                "TEST", "LONG", df=_long_reversion_frame(), include_trades=True
            )
        )
        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(avg_return, 9.31, places=2)
        self.assertEqual(details[0]["entry_date"], "2025-04-04")
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertEqual(details[0]["exit_events"][1]["reason"], "mean reversion")


if __name__ == "__main__":
    unittest.main()
