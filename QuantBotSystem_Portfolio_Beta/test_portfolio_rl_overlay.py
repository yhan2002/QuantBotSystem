from dataclasses import FrozenInstanceError
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import portfolio_rl_overlay as subject


def _ohlcv_frame(periods=105):
    index = pd.bdate_range("2024-01-02", periods=periods)
    step = np.arange(periods, dtype=float)
    close = 100.0 + 0.18 * step + 1.7 * np.sin(step / 6.0)
    open_price = close * (1.0 + 0.002 * np.cos(step / 4.0))
    return pd.DataFrame(
        {
            "Open": open_price,
            "High": np.maximum(open_price, close) * 1.01,
            "Low": np.minimum(open_price, close) * 0.99,
            "Close": close,
            "Volume": 1_000_000.0 * (1.1 + 0.1 * np.sin(step / 5.0)),
        },
        index=index,
    )


def _market_frame(index):
    step = np.arange(len(index), dtype=float)
    adjusted = 75.0 + 0.11 * step + 1.2 * np.cos(step / 8.0)
    return pd.DataFrame(
        {
            "Close": adjusted * 1.5,
            "Adj Close": adjusted,
        },
        index=index,
    )


def _trade(frame, *, side="LONG"):
    return {
        "ticker": "AAA",
        "strategy": "Momentum",
        "side": side,
        "signal_date": frame.index[80].date().isoformat(),
        "entry_date": frame.index[81].date().isoformat(),
        "rank_score": 0.8,
        "requested_weight": 0.04,
        "structure": "TREND",
    }


def _policy(long_bias, short_bias, gross_bias):
    size_count = 1 + len(subject.STATIC_FEATURE_NAMES) + 4
    gross_count = 6
    return subject.ContinuousSizingPolicy(
        (float(long_bias),) + (0.0,) * (size_count - 1),
        (float(short_bias),) + (0.0,) * (size_count - 1),
        (float(gross_bias),) + (0.0,) * (gross_count - 1),
    )


def _signals():
    features = (0.0,) * len(subject.STATIC_FEATURE_NAMES)
    return [
        {
            "trade_id": "LONG-1",
            "ticker": "AAA",
            "side": "LONG",
            "policy_features": features,
        },
        {
            "trade_id": "SHORT-1",
            "ticker": "BBB",
            "side": "SHORT",
            "policy_features": features,
        },
    ]


def _ledger_trade(
    trade_id,
    side,
    *,
    eligible=True,
    allocation_status="FILLED",
    gross_scale=1.0,
    multiplier=1.0,
):
    trade = {
        "trade_id": trade_id,
        "ticker": trade_id,
        "side": side,
        "allocation_status": allocation_status,
        "evaluated_return_pct": 1.0 if side == "LONG" else -0.5,
        "policy_feature_eligible": eligible,
        "policy_gross_scale": gross_scale,
        "policy_trade_multiplier": multiplier,
    }
    if eligible:
        trade["policy_features"] = (0.0,) * len(
            subject.STATIC_FEATURE_NAMES
        )
    return trade


def _default_ledger_trades():
    return [
        *[
            _ledger_trade(
                f"ELIGIBLE-{index}",
                "LONG" if index % 2 == 0 else "SHORT",
            )
            for index in range(8)
        ],
        _ledger_trade("PASSTHROUGH-LONG", "LONG", eligible=False),
        _ledger_trade("PASSTHROUGH-SHORT", "SHORT", eligible=False),
    ]


def _performance(
    strategy_returns,
    market_returns=None,
    *,
    acwi_complete=True,
    ledger_trades=None,
    gross_exposure_pct=40.0,
    net_exposure_pct=0.0,
):
    strategy_returns = np.asarray(strategy_returns, dtype=float)
    if market_returns is None:
        market_returns = np.zeros_like(strategy_returns)
    market_returns = np.asarray(market_returns, dtype=float)
    strategy_nav = np.cumprod(1.0 + strategy_returns)
    roi_pct = float((strategy_nav[-1] - 1.0) * 100.0)
    trades = (
        _default_ledger_trades()
        if ledger_trades is None
        else [dict(trade) for trade in ledger_trades]
    )
    filled_trades = sum(
        trade.get("allocation_status") == "FILLED" for trade in trades
    )
    return {
        "market_benchmark_daily_curve_available": acwi_complete,
        "daily": pd.DataFrame(
            {
                "strategy_return": strategy_returns,
                "market_benchmark_return": market_returns,
                "gross_exposure_pct": np.full(
                    strategy_returns.size,
                    gross_exposure_pct,
                ),
                "net_exposure_pct": np.full(
                    strategy_returns.size,
                    net_exposure_pct,
                ),
            }
        ),
        "strategy": {
            "roi_pct": roi_pct,
            "cagr_pct": roi_pct,
            "mdd_pct": min(0.0, roi_pct),
            "sharpe": 1.0,
        },
        "market_benchmark": {
            "roi_pct": float(
                (np.prod(1.0 + market_returns) - 1.0) * 100.0
            ),
        },
        "ledger": {
            "filled_trades": filled_trades,
            "skipped_trades": len(trades) - filled_trades,
            "trades": trades,
        },
    }


class PointInTimeFeatureTests(unittest.TestCase):
    def setUp(self):
        self.prices = _ohlcv_frame()
        self.market = _market_frame(self.prices.index)
        self.trade = _trade(self.prices)
        self.signal_date = pd.Timestamp(self.trade["signal_date"])

    def test_features_are_invariant_to_ticker_future_suffix(self):
        prefix = self.prices.loc[: self.signal_date].copy()
        changed_future = self.prices.copy()
        future = changed_future.index > self.signal_date
        changed_future.loc[
            future, ["Open", "High", "Low", "Close", "Volume"]
        ] *= 50.0

        prefix_date, prefix_features = subject._raw_feature_vector(
            self.trade,
            prefix,
            self.market,
        )
        full_date, full_features = subject._raw_feature_vector(
            self.trade,
            changed_future,
            self.market,
        )

        self.assertEqual(prefix_date, full_date)
        np.testing.assert_array_equal(prefix_features, full_features)

    def test_acwi_signal_day_and_future_are_strictly_excluded(self):
        baseline = subject._lagged_market_features(
            self.prices,
            self.market,
            self.signal_date,
        )
        changed = self.market.copy()
        changed.loc[
            changed.index >= self.signal_date,
            ["Close", "Adj Close"],
        ] *= 100.0

        actual = subject._lagged_market_features(
            self.prices,
            changed,
            self.signal_date,
        )

        np.testing.assert_array_equal(baseline, actual)

        changed_prior = self.market.copy()
        changed_prior.loc[
            changed_prior.index[79],
            ["Close", "Adj Close"],
        ] *= 1.25
        prior_sensitive = subject._lagged_market_features(
            self.prices,
            changed_prior,
            self.signal_date,
        )
        self.assertFalse(np.allclose(baseline, prior_sensitive))


class FeatureScalerTests(unittest.TestCase):
    def test_schema_metadata_and_frozen_training_transform(self):
        width = len(subject.STATIC_FEATURE_NAMES)
        first = np.arange(width, dtype=float)
        training = np.vstack([first, first + 2.0])
        scaler = subject.FeatureScaler.fit(training)
        expected = np.full(width, 2.0)

        training[:] = 10_000.0

        np.testing.assert_allclose(
            scaler.transform(first + 3.0),
            expected,
        )
        payload = scaler.to_dict()
        self.assertEqual(payload["schema_version"], subject.FEATURE_SCHEMA_VERSION)
        self.assertEqual(
            payload["feature_names"],
            list(subject.STATIC_FEATURE_NAMES),
        )
        with self.assertRaises(FrozenInstanceError):
            scaler.means = (0.0,) * width
        with self.assertRaisesRegex(ValueError, "wrong width"):
            scaler.transform(np.zeros(width - 1))

    def test_preparation_rejects_a_scaler_from_another_schema(self):
        prices = _ohlcv_frame()
        width = len(subject.STATIC_FEATURE_NAMES)
        wrong_scaler = subject.FeatureScaler.fit(
            np.vstack([np.zeros(width), np.ones(width)]),
            feature_names=tuple(f"other_{index}" for index in range(width)),
        )

        with self.assertRaisesRegex(ValueError, "schema does not match"):
            subject.prepare_point_in_time_trades(
                [_trade(prices)],
                {"AAA": prices},
                _market_frame(prices.index),
                scaler=wrong_scaler,
            )

    def test_insufficient_feature_trade_is_retained_as_passthrough(self):
        prices = _ohlcv_frame()
        eligible = {
            **_trade(prices),
            "trade_id": "ELIGIBLE",
        }
        passthrough = {
            **eligible,
            "trade_id": "PASSTHROUGH",
            "ticker": "BBB",
        }

        prepared = subject.prepare_point_in_time_trades(
            [eligible, passthrough],
            {
                "AAA": prices,
                "BBB": prices.iloc[:10],
            },
            _market_frame(prices.index),
        )

        self.assertEqual(
            [trade["trade_id"] for trade in prepared.trades],
            ["ELIGIBLE", "PASSTHROUGH"],
        )
        self.assertIs(prepared.trades[0]["policy_feature_eligible"], True)
        self.assertIn("policy_features", prepared.trades[0])
        self.assertIs(prepared.trades[1]["policy_feature_eligible"], False)
        self.assertNotIn("policy_features", prepared.trades[1])
        self.assertEqual(prepared.trades[1]["side"], passthrough["side"])
        self.assertEqual(len(prepared.skipped), 1)
        self.assertEqual(prepared.skipped[0]["ticker"], "BBB")
        self.assertIn(
            "insufficient close-known ticker history",
            prepared.skipped[0]["reason"],
        )


class SizingPolicyTests(unittest.TestCase):
    def test_continuous_policy_uses_long_and_short_heads_with_bounded_outputs(self):
        policy = _policy(long_bias=20.0, short_bias=-20.0, gross_bias=0.5)

        decision = policy.decide(_signals(), {})

        self.assertGreater(decision["trade_multipliers"]["LONG-1"], 0.99)
        self.assertLess(decision["trade_multipliers"]["SHORT-1"], 0.01)
        for value in (
            decision["gross_scale"],
            *decision["trade_multipliers"].values(),
        ):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_ensemble_returns_the_component_arithmetic_mean(self):
        signals = _signals()
        state = {
            "gross_fraction": 0.25,
            "net_fraction": -0.1,
            "long_fraction": 0.2,
            "short_fraction": 0.3,
            "active_positions": 4,
        }
        policies = (
            _policy(long_bias=4.0, short_bias=-3.0, gross_bias=2.0),
            _policy(long_bias=-2.0, short_bias=3.0, gross_bias=-1.0),
        )
        components = [policy.decide(signals, state) for policy in policies]

        actual = subject.EnsembleSizingPolicy(policies).decide(signals, state)

        self.assertAlmostEqual(
            actual["gross_scale"],
            np.mean([item["gross_scale"] for item in components]),
        )
        for trade_id in ("LONG-1", "SHORT-1"):
            self.assertAlmostEqual(
                actual["trade_multipliers"][trade_id],
                np.mean(
                    [
                        item["trade_multipliers"][trade_id]
                        for item in components
                    ]
                ),
            )
            self.assertGreaterEqual(actual["trade_multipliers"][trade_id], 0.0)
            self.assertLessEqual(actual["trade_multipliers"][trade_id], 1.0)

    def test_mixed_passthrough_cohort_preserves_gross_and_only_scales_eligible(self):
        eligible = {
            "trade_id": "ELIGIBLE",
            "ticker": "AAA",
            "side": "LONG",
            "policy_feature_eligible": True,
            "policy_features": (0.0,) * len(subject.STATIC_FEATURE_NAMES),
        }
        passthrough = {
            "trade_id": "PASSTHROUGH",
            "ticker": "BBB",
            "side": "SHORT",
            "policy_feature_eligible": False,
        }
        policy = _policy(
            long_bias=-4.0,
            short_bias=20.0,
            gross_bias=-20.0,
        )

        decision = policy.decide([eligible, passthrough], {})

        self.assertEqual(decision["gross_scale"], 1.0)
        self.assertEqual(
            decision["trade_multipliers"]["PASSTHROUGH"],
            1.0,
        )
        self.assertAlmostEqual(
            decision["trade_multipliers"]["ELIGIBLE"],
            subject._sigmoid(-4.0),
        )
        self.assertNotEqual(
            decision["trade_multipliers"]["ELIGIBLE"],
            1.0,
        )


class FeasibilityTests(unittest.TestCase):
    def test_zero_multiplier_skipped_candidate_remains_in_collapse_denominator(
        self,
    ):
        baseline_trades = [
            _ledger_trade("LONG-FILLED", "LONG"),
            _ledger_trade("LONG-SECOND", "LONG"),
        ]
        candidate_trades = [
            _ledger_trade("LONG-FILLED", "LONG"),
            _ledger_trade(
                "LONG-ZERO",
                "LONG",
                allocation_status="SKIPPED_POLICY_ZERO",
                multiplier=0.0,
            ),
        ]
        baseline = subject.policy_metrics(
            _performance([0.01, -0.01], ledger_trades=baseline_trades)
        )
        candidate = subject.policy_metrics(
            _performance([0.01, -0.01], ledger_trades=candidate_trades)
        )
        config = subject.EvolutionStrategyConfig(
            iterations=1,
            population=1,
            minimum_filled_trades=1,
            minimum_feature_eligible_filled_trades=1,
            minimum_feature_eligible_fill_ratio=0.5,
            minimum_side_feature_eligible_candidates=10,
            minimum_average_gross_ratio=0.1,
            minimum_eligible_effective_scale=0.75,
        )

        feasibility = subject.policy_feasibility(
            candidate,
            baseline,
            config,
        )

        self.assertEqual(candidate["feature_eligible_candidates"], 2)
        self.assertEqual(candidate["feature_eligible_filled_trades"], 1)
        self.assertAlmostEqual(
            candidate["eligible_average_effective_scale"],
            0.5,
        )
        self.assertFalse(feasibility["satisfied"])
        self.assertIn(
            "minimum_eligible_effective_scale",
            feasibility["failed_checks"],
        )

    def test_ensemble_feasibility_applies_side_cvar_and_gross_gates(self):
        baseline = subject.policy_metrics(
            _performance([0.01, -0.01, 0.01])
        )
        candidate = dict(baseline)
        candidate.update(
            {
                "cvar": baseline["cvar"] + 0.01,
                "average_gross_pct": baseline["average_gross_pct"] * 0.2,
                "eligible_long_filled_trades": 0,
                "eligible_long_average_effective_scale": 0.1,
            }
        )
        config = subject.EvolutionStrategyConfig(
            iterations=1,
            population=1,
            minimum_filled_trades=1,
            minimum_feature_eligible_filled_trades=1,
            minimum_feature_eligible_fill_ratio=0.5,
            minimum_side_feature_eligible_candidates=1,
            minimum_average_gross_ratio=0.5,
            minimum_eligible_effective_scale=0.25,
        )

        feasibility = subject.policy_feasibility(
            candidate,
            baseline,
            config,
        )

        self.assertFalse(feasibility["satisfied"])
        self.assertEqual(
            set(feasibility["failed_checks"]),
            {
                "cvar",
                "minimum_average_gross",
                "long_fill_retention",
                "long_effective_scale",
            },
        )
        self.assertTrue(feasibility["checks"]["short_fill_retention"])
        self.assertTrue(feasibility["checks"]["short_effective_scale"])


class RewardAndTrainingTests(unittest.TestCase):
    def test_downside_cvar_uses_exactly_five_of_one_hundred_at_95pct(self):
        returns = np.full(100, 0.01)
        returns[:6] = [-0.50, -0.40, -0.30, -0.20, -0.10, -0.05]

        actual = subject.downside_cvar(returns, confidence=0.95)

        self.assertAlmostEqual(actual, 0.30)

    def test_downside_cvar_and_complete_acwi_requirement(self):
        self.assertAlmostEqual(
            subject.downside_cvar([-0.10, -0.04, 0.01, 0.03], confidence=0.75),
            0.10,
        )
        with self.assertRaisesRegex(ValueError, "daily returns are required"):
            subject.downside_cvar([np.nan, np.inf])
        with self.assertRaisesRegex(ValueError, "complete daily ACWI"):
            subject.policy_metrics(
                _performance([0.01, -0.02], acwi_complete=False)
            )

        incomplete = _performance([0.01, -0.02], [0.0, np.nan])
        with self.assertRaisesRegex(ValueError, "incomplete daily return curve"):
            subject.policy_metrics(incomplete)

    def test_train_policy_is_deterministic_for_a_seed_with_mocked_replays(self):
        weights = np.linspace(
            -1.0,
            1.0,
            subject.ContinuousSizingPolicy.initial().to_vector().size,
        )

        def replay(*args, **kwargs):
            policy = kwargs.get("sizing_policy")
            if policy is None:
                policy_signal = 0.0
            else:
                policy_signal = float(
                    np.tanh(np.dot(policy.to_vector(), weights) / weights.size)
                )
            return _performance(
                [0.002 + 0.0005 * policy_signal, -0.001, 0.001],
                [0.0002, 0.0001, 0.0002],
            )

        config = subject.EvolutionStrategyConfig(
            iterations=2,
            population=2,
            noise_std=0.05,
            learning_rate=0.02,
            l2_penalty=0.0,
            minimum_filled_trades=1,
            minimum_feature_eligible_filled_trades=1,
        )
        kwargs = {
            "price_loader": mock.Mock(),
            "market_benchmark_loader": mock.Mock(),
            "config": config,
            "seed": 37,
        }

        with mock.patch.object(
            subject,
            "evaluate_policy",
            side_effect=replay,
        ) as evaluate:
            first = subject.train_policy(
                [],
                ["AAA"],
                "2024-01-01",
                "2024-12-31",
                **kwargs,
            )
            second = subject.train_policy(
                [],
                ["AAA"],
                "2024-01-01",
                "2024-12-31",
                **kwargs,
            )

        np.testing.assert_array_equal(
            first.policy.to_vector(),
            second.policy.to_vector(),
        )
        self.assertEqual(first.history, second.history)
        self.assertEqual(first.trained_metrics, second.trained_metrics)
        expected_calls_per_run = 3 + config.iterations * (
            2 * config.population + 1
        )
        self.assertEqual(evaluate.call_count, expected_calls_per_run * 2)

    def test_train_policy_selects_best_feasible_before_violating_final_theta(
        self,
    ):
        responses = iter(
            [
                _performance([0.001, -0.010, 0.001]),
                _performance([0.020, -0.005, 0.001]),
                _performance([0.040, -0.005, 0.001]),
                _performance([0.001, -0.005, 0.001]),
                _performance([0.050, -0.020, 0.001]),
                _performance([0.020, -0.005, 0.001]),
            ]
        )
        evaluated_vectors = []

        def replay(*args, **kwargs):
            policy = kwargs.get("sizing_policy")
            evaluated_vectors.append(
                None if policy is None else policy.to_vector().copy()
            )
            return next(responses)

        config = subject.EvolutionStrategyConfig(
            iterations=1,
            population=1,
            noise_std=0.05,
            learning_rate=0.05,
            l2_penalty=0.0,
            minimum_filled_trades=1,
            minimum_feature_eligible_filled_trades=1,
        )
        with mock.patch.object(
            subject,
            "evaluate_policy",
            side_effect=replay,
        ):
            result = subject.train_policy(
                [],
                ["AAA"],
                "2024-01-01",
                "2024-12-31",
                price_loader=mock.Mock(),
                market_benchmark_loader=mock.Mock(),
                config=config,
                seed=11,
            )

        initial = subject.ContinuousSizingPolicy.initial().to_vector()
        self.assertEqual(result.selected_iteration, 0)
        np.testing.assert_array_equal(result.policy.to_vector(), initial)
        np.testing.assert_array_equal(evaluated_vectors[1], initial)
        self.assertFalse(
            np.array_equal(evaluated_vectors[4], evaluated_vectors[1])
        )
        np.testing.assert_array_equal(
            evaluated_vectors[-1],
            evaluated_vectors[1],
        )
        self.assertFalse(result.history[0]["feasible"])
        self.assertFalse(result.history[0]["selected_as_best"])

    def test_signed_dual_history_records_used_next_and_relaxes(self):
        responses = iter(
            [
                _performance([0.001, -0.010, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.020, -0.020, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.010, -0.005, 0.001]),
                _performance([0.010, -0.005, 0.001]),
            ]
        )
        config = subject.EvolutionStrategyConfig(
            iterations=2,
            population=1,
            noise_std=0.05,
            learning_rate=0.02,
            l2_penalty=0.0,
            dual_learning_rate=10.0,
            minimum_filled_trades=1,
            minimum_feature_eligible_filled_trades=1,
        )

        with mock.patch.object(
            subject,
            "evaluate_policy",
            side_effect=lambda *args, **kwargs: next(responses),
        ):
            result = subject.train_policy(
                [],
                ["AAA"],
                "2024-01-01",
                "2024-12-31",
                price_loader=mock.Mock(),
                market_benchmark_loader=mock.Mock(),
                config=config,
                seed=23,
            )

        first, second = result.history
        self.assertEqual(first["dual_multiplier_used"], 1.0)
        self.assertGreater(
            first["dual_multiplier_next"],
            first["dual_multiplier_used"],
        )
        self.assertAlmostEqual(
            second["dual_multiplier_used"],
            first["dual_multiplier_next"],
        )
        self.assertLess(second["cvar_constraint_gap"], 0.0)
        self.assertEqual(second["cvar_violation"], 0.0)
        self.assertLess(
            second["dual_multiplier_next"],
            second["dual_multiplier_used"],
        )
        self.assertAlmostEqual(
            result.final_dual_multiplier,
            second["dual_multiplier_next"],
        )


if __name__ == "__main__":
    unittest.main()
