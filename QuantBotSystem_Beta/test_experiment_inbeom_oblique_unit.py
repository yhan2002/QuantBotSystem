import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import pandas as pd

import test_experiment_inbeom_oblique as experiment


def _daily_frame(weeks=12):
    index = pd.bdate_range("2025-01-06", periods=weeks * 5)
    values = []
    for number, _ in enumerate(index):
        price = 100 + number * 0.2
        values.append({"Open": price, "High": price + 1, "Low": price - 1, "Close": price, "Volume": 1000})
    return pd.DataFrame(values, index=index)


class InbeomObliqueExperimentTests(unittest.TestCase):
    def test_weekly_pivot_is_confirmed_only_after_right_side_weeks(self):
        weekly = pd.DataFrame(
            {"Open": [9, 10, 9, 8, 9], "High": [10, 14, 11, 9, 10], "Low": [8, 9, 7, 6, 7], "Close": [9, 10, 8, 7, 9]},
            index=pd.date_range("2025-01-03", periods=5, freq="W-FRI"),
        )
        pivots = experiment.confirmed_pivots(weekly, "High", pivot_span=1)
        self.assertEqual(pivots[0]["pivot_idx"], 1)
        self.assertEqual(pivots[0]["confirmed_idx"], 2)

    def test_high_high_channel_uses_later_high_for_equal_spacing(self):
        pivots = [
            {"pivot_idx": 1, "confirmed_idx": 2, "price": 100.0},
            {"pivot_idx": 5, "confirmed_idx": 6, "price": 120.0},
            {"pivot_idx": 9, "confirmed_idx": 10, "price": 180.0},
        ]
        channel = experiment.latest_historical_high_channel(
            pivots,
            as_of_idx=10,
            min_anchor_separation_weeks=2,
            min_channel_width_pct=0.01,
        )
        self.assertIsNotNone(channel)
        self.assertAlmostEqual(experiment._line_value(channel, 9, level=1), 180.0, places=6)
        self.assertAlmostEqual(experiment._line_value(channel, 13, level=2), 240.0, places=6)
        log_channel = experiment.latest_historical_high_channel(
            pivots,
            as_of_idx=10,
            min_anchor_separation_weeks=2,
            min_channel_width_pct=0.01,
            channel_scale="log",
        )
        self.assertAlmostEqual(experiment._line_value(log_channel, 13, level=2), 270.0, places=6)

    def test_entry_risk_reward_is_calculated_for_screener_reference(self):
        line = {
            "first": {"pivot_idx": 1, "price": 100.0},
            "second": {"pivot_idx": 5, "price": 120.0},
            "slope": 5.0,
            "spacing": 40.0,
        }
        weekly = pd.DataFrame({"ATR": [4.0] * 10})
        quality = experiment.entry_risk_reward(weekly, line, level=1, idx=9, entry_price=181.0)
        self.assertIsNotNone(quality)
        self.assertGreaterEqual(quality["risk_reward"], 4.0)

    def test_screen_snapshot_exposes_drawable_high_anchors_and_channel_basis(self):
        weekly = pd.DataFrame(
            {"Close": [100.0] * 10, "ATR": [2.0] * 10},
            index=pd.date_range("2020-01-03", periods=10, freq="W-FRI"),
        )
        line = {
            "first": {"pivot_idx": 1, "price": 100.0},
            "second": {"pivot_idx": 5, "price": 120.0},
            "third": {"pivot_idx": 9, "price": 180.0},
            "slope": 5.0,
            "spacing": 40.0,
        }
        snapshot = experiment._screen_snapshot("TEST", weekly, line, 1, 9, 8, "WATCH")
        self.assertEqual(snapshot["base_anchors"][0]["date"], "2020-01-10")
        self.assertEqual(snapshot["base_anchors"][1]["kind"], "HIGH")
        self.assertEqual(snapshot["channel_reference"]["date"], "2020-03-06")
        self.assertEqual(snapshot["support_channel"]["level"], 1)

    def test_terminal_hides_zero_trade_ticker_rows(self):
        closed = [{"return": 2.0, "holding_days": 5, "entry_date": "2025-01-01", "exit_date": "2025-01-08", "reason": "test", "support_level": 1}]
        with patch.object(experiment.core_market_data_store, "get_price_data", return_value=_daily_frame()), patch.object(
            experiment,
            "run_inbeom_oblique_backtest",
            side_effect=[{"closed_trades": closed, "open_positions": [], "history_weeks": 600}, {"closed_trades": [], "open_positions": [], "history_weeks": 600}],
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                experiment.run_terminal_experiment(["ONE", "ZERO"])
        self.assertIn("ONE: trades 1", output.getvalue())
        self.assertNotIn("ZERO:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
