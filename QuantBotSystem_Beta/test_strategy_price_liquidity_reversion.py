import math
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
        self.assertGreaterEqual(
            result["planned_rr"],
            strategy_price_liquidity_reversion.REVERSION_MIN_PLANNED_RR,
        )

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

    def test_planned_blended_rr_matches_scale_out_weights_for_both_sides(self):
        daily_volatility = 0.04 / math.sqrt(
            strategy_price_liquidity_reversion.REVERSION_SHOCK_DAYS
        )

        long_rr = strategy_price_liquidity_reversion._planned_blended_rr(
            "LONG", 94.0, 100.0, daily_volatility, 1.0
        )
        short_rr = strategy_price_liquidity_reversion._planned_blended_rr(
            "SHORT", 106.0, 100.0, daily_volatility, 1.0
        )

        self.assertAlmostEqual(long_rr, 10.0 / 3.0)
        self.assertAlmostEqual(short_rr, 10.0 / 3.0)

    def test_planned_reward_is_zero_after_mean_target_is_already_passed(self):
        daily_volatility = 0.04 / math.sqrt(
            strategy_price_liquidity_reversion.REVERSION_SHOCK_DAYS
        )

        long_rr = strategy_price_liquidity_reversion._planned_blended_rr(
            "LONG", 101.0, 100.0, daily_volatility, 1.0
        )
        short_rr = strategy_price_liquidity_reversion._planned_blended_rr(
            "SHORT", 99.0, 100.0, daily_volatility, 1.0
        )

        self.assertEqual(long_rr, 0.0)
        self.assertEqual(short_rr, 0.0)

    def test_low_planned_rr_blocks_otherwise_valid_long_signal(self):
        baseline = _long_reversion_frame().iloc[:67].copy()
        baseline_features = strategy_price_liquidity_reversion._add_reversion_features(
            baseline
        )
        self.assertTrue(bool(baseline_features.iloc[-1]["LONG_SIGNAL"]))

        high_risk = baseline.copy()
        for date in high_risk.index[-14:]:
            day_open = float(high_risk.at[date, "Open"])
            day_close = float(high_risk.at[date, "Close"])
            high_risk.at[date, "High"] = max(day_open, day_close) + 10.0
            high_risk.at[date, "Low"] = min(day_open, day_close) - 10.0
        last_date = high_risk.index[-1]
        last_open = float(high_risk.at[last_date, "Open"])
        last_close = float(high_risk.at[last_date, "Close"])
        high_risk.at[last_date, "High"] = last_close + 4.0
        high_risk.at[last_date, "Low"] = min(last_open, last_close) - 6.0

        high_risk_features = strategy_price_liquidity_reversion._add_reversion_features(
            high_risk
        )
        latest = high_risk_features.iloc[-1]
        self.assertLess(
            latest["LONG_PLANNED_RR"],
            strategy_price_liquidity_reversion.REVERSION_MIN_PLANNED_RR,
        )
        self.assertFalse(bool(latest["LONG_SIGNAL"]))

    def test_planned_rr_signal_does_not_use_next_day_prices(self):
        original = _long_reversion_frame().iloc[:68].copy()
        changed_future = original.copy()
        changed_future.iloc[-1] = [200.0, 220.0, 180.0, 210.0, 999999.0]
        signal_date = original.index[-2]

        original_features = strategy_price_liquidity_reversion._add_reversion_features(
            original
        )
        changed_features = strategy_price_liquidity_reversion._add_reversion_features(
            changed_future
        )

        self.assertEqual(
            bool(original_features.at[signal_date, "LONG_SIGNAL"]),
            bool(changed_features.at[signal_date, "LONG_SIGNAL"]),
        )
        self.assertAlmostEqual(
            original_features.at[signal_date, "LONG_PLANNED_RR"],
            changed_features.at[signal_date, "LONG_PLANNED_RR"],
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
        self.assertGreaterEqual(
            details[0]["planned_rr"],
            strategy_price_liquidity_reversion.REVERSION_MIN_PLANNED_RR,
        )
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertEqual(details[0]["exit_events"][1]["reason"], "mean reversion")


if __name__ == "__main__":
    unittest.main()
