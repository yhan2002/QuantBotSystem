import unittest

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
        avg_return, win_rate, count, _, details = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST", "LONG", df=_long_reversion_frame(), include_trades=True
            )
        )
        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(avg_return, 9.31, places=2)
        self.assertEqual(details[0]["entry_date"], "2025-04-04")
        self.assertEqual(details[0]["entry_price"], 102.0)
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertEqual(details[0]["exit_events"][1]["reason"], "mean reversion")


if __name__ == "__main__":
    unittest.main()
