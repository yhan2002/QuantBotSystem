import unittest

import numpy as np
import pandas as pd

from core_portfolio_ledger import simulate_portfolio
import portfolio_hmm_overlay as subject


def _feature_frame(rows=90):
    index = pd.date_range("2024-01-01", periods=rows, freq="B")
    phase = np.arange(rows, dtype=float)
    noise = np.random.default_rng(7)
    trend = np.where(phase < rows / 2, 0.3, 2.0)
    return pd.DataFrame(
        {
            "market_return_20d": (
                np.sin(phase / 8.0) * trend / 100.0
                + noise.normal(0.0, 0.001, rows)
            ),
            "realized_volatility_20d": (
                np.where(phase < rows / 2, 0.22, 0.12)
                + noise.normal(0.0, 0.005, rows)
            ),
            "trend_strength_20d": (
                trend + 0.05 * np.sin(phase) + noise.normal(0.0, 0.02, rows)
            ),
            "breadth_20d": (
                np.where(phase < rows / 2, 0.50, 0.72)
                + noise.normal(0.0, 0.01, rows)
            ),
            "dispersion_20d": (
                np.where(phase < rows / 2, 0.05, 0.025)
                + noise.normal(0.0, 0.002, rows)
            ),
        },
        index=index,
    )


class RegimeFeatureTests(unittest.TestCase):
    def test_appending_future_prices_does_not_change_past_features(self):
        index = pd.date_range("2024-01-01", periods=80, freq="B")
        market = pd.Series(100.0 + np.arange(80) * 0.3, index=index)
        universe = pd.DataFrame(
            {
                "AAA": 50.0 + np.arange(80) * 0.2,
                "BBB": 70.0 + np.sin(np.arange(80) / 4.0),
                "CCC": 90.0 - np.arange(80) * 0.05,
            },
            index=index,
        )
        short = subject.build_regime_features(market.iloc[:60], universe.iloc[:60])
        full = subject.build_regime_features(market, universe)
        pd.testing.assert_frame_equal(short, full.loc[short.index])

    def test_rule_comparator_uses_only_past_thresholds(self):
        frame = _feature_frame()
        short = subject.rule_trend_probabilities(frame.iloc[:70], min_history=20)
        full = subject.rule_trend_probabilities(frame, min_history=20)
        pd.testing.assert_series_equal(short, full.loc[short.index])


class GaussianHMMTests(unittest.TestCase):
    def test_walk_forward_probabilities_are_deterministic_and_point_in_time(self):
        frame = _feature_frame()
        kwargs = {"min_train_size": 30, "refit_interval": 10}
        short = subject.walk_forward_trend_probabilities(frame.iloc[:70], **kwargs)
        full = subject.walk_forward_trend_probabilities(frame, **kwargs)
        repeat = subject.walk_forward_trend_probabilities(frame, **kwargs)
        pd.testing.assert_series_equal(short, full.loc[short.index])
        pd.testing.assert_series_equal(full, repeat)
        eligible = full.dropna()
        self.assertTrue(eligible.between(0.0, 1.0).all())
        self.assertEqual(len(eligible), len(frame) - kwargs["min_train_size"])
        self.assertGreater(full.iloc[60:].mean(), full.iloc[30:45].mean())

    def test_invalid_training_contract_is_rejected(self):
        frame = _feature_frame(20)
        with self.assertRaisesRegex(ValueError, "min_train_size"):
            subject.walk_forward_trend_probabilities(frame, min_train_size=9)
        with self.assertRaisesRegex(ValueError, "train_window"):
            subject.walk_forward_trend_probabilities(
                frame,
                min_train_size=10,
                train_window=9,
            )


class RegimeSizingPolicyTests(unittest.TestCase):
    def test_missing_probability_reproduces_baseline_ledger(self):
        dates = pd.to_datetime(["2026-01-02", "2026-01-05"])
        prices = pd.DataFrame(
            {
                "Open": [100.0, 110.0],
                "High": [100.0, 110.0],
                "Low": [100.0, 110.0],
                "Close": [100.0, 110.0],
                "Volume": [1000, 1000],
            },
            index=dates,
        )
        trade = {
            "ticker": "AAA",
            "strategy": "Momentum",
            "side": "LONG",
            "signal_date": "2026-01-01",
            "entry_date": "2026-01-02",
            "entry_price": 100.0,
            "signal_score": 2.0,
            "exit_events": [
                {"date": "2026-01-05", "weight": 1.0, "price": 110.0}
            ],
        }
        arguments = {
            "trades": [trade],
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
            "price_loader": lambda ticker: prices,
        }
        baseline = simulate_portfolio(**arguments)
        routed = simulate_portfolio(
            **arguments,
            entry_sizing_policy=subject.RegimeSizingPolicy({}),
        )
        pd.testing.assert_frame_equal(baseline["daily"], routed["daily"])
        self.assertEqual(
            baseline["trades"][0]["allocated_base_notional"],
            routed["trades"][0]["allocated_base_notional"],
        )

    def test_router_preserves_each_side_requested_gross(self):
        date = pd.Timestamp("2026-01-05")
        policy = subject.RegimeSizingPolicy({date: 0.8}, floor=0.25)
        signals = [
            {
                "trade_id": "M",
                "strategy": "Momentum",
                "side": "LONG",
                "signal_date": date,
                "requested_weight": 0.03,
            },
            {
                "trade_id": "B",
                "strategy": "BOX_RANGE",
                "side": "LONG",
                "signal_date": date,
                "requested_weight": 0.02,
            },
            {
                "trade_id": "R",
                "strategy": "REVERSION",
                "side": "SHORT",
                "signal_date": date,
                "requested_weight": 0.04,
            },
            {
                "trade_id": "X",
                "strategy": "UNKNOWN",
                "side": "LONG",
                "signal_date": date,
                "requested_weight": 0.01,
            },
        ]
        decision = policy(signals=signals, portfolio_state={})
        multipliers = decision["trade_multipliers"]
        self.assertEqual(decision["gross_scale"], 1.0)
        self.assertGreater(multipliers["M"], multipliers["B"])
        self.assertGreater(multipliers["M"], 0.0)
        self.assertGreater(multipliers["B"], 0.0)
        self.assertAlmostEqual(
            0.03 * multipliers["M"] + 0.02 * multipliers["B"],
            0.05,
        )
        self.assertEqual(multipliers["R"], 1.0)
        self.assertEqual(multipliers["X"], 1.0)

    def test_missing_regime_probability_is_exact_baseline(self):
        policy = subject.RegimeSizingPolicy({}, floor=0.25)
        signals = [
            {
                "trade_id": "M",
                "strategy": "MOMENTUM",
                "side": "LONG",
                "signal_date": "2026-01-05",
                "requested_weight": 0.05,
            }
        ]
        self.assertEqual(
            policy(signals=signals, portfolio_state={}),
            {"gross_scale": 1.0, "trade_multipliers": {"M": 1.0}},
        )

    def test_router_preserves_gross_when_preferred_name_starts_at_cap(self):
        date = pd.Timestamp("2026-01-05")
        policy = subject.RegimeSizingPolicy({date: 1.0}, floor=0.25)
        signals = [
            {
                "trade_id": "M",
                "strategy": "Momentum",
                "side": "LONG",
                "signal_date": date,
                "requested_weight": 0.05,
            },
            {
                "trade_id": "B",
                "strategy": "Box Range",
                "side": "LONG",
                "signal_date": date,
                "requested_weight": 0.01,
            },
        ]
        multipliers = policy(
            signals=signals,
            portfolio_state={},
        )["trade_multipliers"]
        targets = {
            signal["trade_id"]: signal["requested_weight"]
            * multipliers[signal["trade_id"]]
            for signal in signals
        }
        self.assertAlmostEqual(sum(targets.values()), 0.06)
        self.assertLessEqual(max(targets.values()), 0.05)
        self.assertAlmostEqual(targets["M"], 0.05)
        self.assertAlmostEqual(targets["B"], 0.01)

    def test_probability_and_floor_validation(self):
        with self.assertRaisesRegex(ValueError, "floor"):
            subject.RegimeSizingPolicy({}, floor=-0.1)
        with self.assertRaisesRegex(ValueError, "probabilities"):
            subject.RegimeSizingPolicy({"2026-01-05": 1.1})
        with self.assertRaisesRegex(ValueError, "max_name_weight"):
            subject.RegimeSizingPolicy({}, max_name_weight=0.0)


if __name__ == "__main__":
    unittest.main()
