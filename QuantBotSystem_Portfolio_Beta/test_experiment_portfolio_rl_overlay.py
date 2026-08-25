import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd

import experiment_portfolio_rl_overlay as subject


def _metrics(strategy_roi, acwi_roi):
    return {
        "roi_pct": strategy_roi,
        "cagr_pct": strategy_roi,
        "mdd_pct": -1.0,
        "sharpe": 1.0,
        "cvar": 0.01,
        "acwi_roi_pct": acwi_roi,
        "excess_log_return": 0.02,
        "filled_trades": 12,
        "skipped_trades": 1,
        "average_gross_pct": 40.0,
        "maximum_gross_pct": 75.0,
    }


class ExperimentHelperTests(unittest.TestCase):
    def test_checkpoint_failure_and_early_paths_have_distinct_file_behavior(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "active_checkpoint.json"

            @subject._checkpoint_failures
            def fail_after_checkpoint():
                subject._ACTIVE_CHECKPOINT_PATH = checkpoint
                subject._write_json_atomic(
                    checkpoint,
                    {
                        "status": "STARTED",
                        "phase": "TEST_PHASE",
                    },
                )
                raise RuntimeError("synthetic failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                fail_after_checkpoint()

            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAILED")
            self.assertEqual(payload["previous_status"], "STARTED")
            self.assertEqual(payload["failure_phase"], "TEST_PHASE")
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertEqual(payload["error"], "synthetic failure")
            self.assertIsNone(subject._ACTIVE_CHECKPOINT_PATH)

            validation_dir = root / "validation"
            with self.assertRaisesRegex(
                ValueError,
                "universe groups must be unique",
            ):
                subject.run_experiment(
                    groups=("DUPLICATE", "DUPLICATE"),
                    strategies=("MA",),
                    seeds=(11,),
                    output_dir=validation_dir,
                    timestamp="validation",
                )
            self.assertFalse(validation_dir.exists())

            estimate_dir = root / "estimate"
            with (
                mock.patch.object(
                    subject,
                    "_universe_specs",
                    return_value={"GROUP": ["AAA"]},
                ),
                mock.patch.object(
                    subject.config,
                    "universe_groups",
                    {"GROUP": ["AAA"]},
                ),
                mock.patch("builtins.print"),
            ):
                subject.run_experiment(
                    groups=("GROUP",),
                    strategies=("MA",),
                    seeds=(11,),
                    output_dir=estimate_dir,
                    timestamp="estimate",
                    estimate_only=True,
                )
            self.assertFalse(estimate_dir.exists())

    def test_strategy_aliases_are_normalized(self):
        aliases = {
            "momentum": "Momentum",
            "MA": "Momentum",
            "box": "Box Range",
            "box range": "Box Range",
            "BOX_RANGE": "Box Range",
            "reversion": "Reversion",
        }
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(subject._strategy_name(alias), expected)
        with self.assertRaisesRegex(ValueError, "Unknown strategy"):
            subject._strategy_name("not-a-strategy")

    def test_duplicate_seeds_fail_before_universe_or_local_data_access(self):
        with (
            mock.patch.object(subject, "_universe_specs") as universe_specs,
            mock.patch.object(
                subject.core_market_data_store,
                "get_price_data",
            ) as get_price_data,
        ):
            with self.assertRaisesRegex(ValueError, "seeds must be unique"):
                subject.run_experiment(
                    groups=("DO_NOT_LOAD",),
                    strategies=("MA",),
                    seeds=(7, 7),
                )

        universe_specs.assert_not_called()
        get_price_data.assert_not_called()

    def test_duplicate_groups_and_alias_equivalent_strategies_fail_early(self):
        with (
            mock.patch.object(subject, "_universe_specs") as universe_specs,
            mock.patch.object(
                subject.core_market_data_store,
                "get_price_data",
            ) as get_price_data,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "universe groups must be unique",
            ):
                subject.run_experiment(
                    groups=("CSQ_UNIV", "CSQ_UNIV"),
                    strategies=("MA",),
                    seeds=(7,),
                )
            with self.assertRaisesRegex(
                ValueError,
                "unique after alias normalization",
            ):
                subject.run_experiment(
                    groups=("CSQ_UNIV",),
                    strategies=("MOMENTUM", "MA"),
                    seeds=(7,),
                )

        universe_specs.assert_not_called()
        get_price_data.assert_not_called()

    def test_universe_specs_combines_sorted_deduplicated_non_acwi_members(self):
        benchmark = (
            subject.core_portfolio_performance.MARKET_BENCHMARK_TICKER
        )
        universe_groups = {
            "CSQ_UNIV": ["ZZZ", "DUP", benchmark],
            "US_LARGE_CAP_100_20260630_PROXY": [
                "DUP",
                benchmark,
                "AAA",
            ],
        }
        with mock.patch.object(
            subject.config,
            "universe_groups",
            universe_groups,
        ):
            actual = subject._universe_specs(
                (
                    "CSQ_UNIV",
                    "US_LARGE_CAP_100_20260630_PROXY",
                ),
                combine_groups=True,
                combined_universe_name="CSQ_US100",
            )

        self.assertEqual(
            actual,
            {"CSQ_US100": ["AAA", "DUP", "ZZZ"]},
        )

    def test_combined_universe_options_fail_before_data_or_output_access(self):
        with (
            mock.patch.object(subject, "_universe_specs") as universe_specs,
            mock.patch.object(
                subject.core_market_data_store,
                "get_price_data",
            ) as get_price_data,
            mock.patch.object(subject, "_prepare_output_paths") as output_paths,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "combine_groups requires at least two",
            ):
                subject.run_experiment(
                    groups=("CSQ_UNIV",),
                    combine_groups=True,
                    strategies=("BOX",),
                    seeds=(11,),
                )
            with self.assertRaisesRegex(
                ValueError,
                "combined_universe_name requires combine_groups=True",
            ):
                subject.run_experiment(
                    groups=("CSQ_UNIV",),
                    combined_universe_name="CSQ_US100",
                    strategies=("BOX",),
                    seeds=(11,),
                )

        universe_specs.assert_not_called()
        get_price_data.assert_not_called()
        output_paths.assert_not_called()

    def test_estimate_only_calculates_replays_without_loading_market_data(self):
        config = subject.portfolio_rl_overlay.EvolutionStrategyConfig(
            iterations=2,
            population=1,
        )
        with (
            mock.patch.object(
                subject,
                "_universe_specs",
                return_value={"G1": ["AAA"], "G2": ["BBB"]},
            ),
            mock.patch.object(
                subject.config,
                "universe_groups",
                {"G1": ["AAA"], "G2": ["BBB"]},
            ),
            mock.patch.object(
                subject.core_market_data_store,
                "get_price_data",
            ) as get_price_data,
            mock.patch.object(subject, "_prepare_output_paths") as output_paths,
            mock.patch("builtins.print"),
        ):
            result = subject.run_experiment(
                groups=("G1", "G2"),
                strategies=("MA", "BOX"),
                seeds=(11, 23),
                training_config=config,
                estimate_only=True,
            )

        self.assertEqual(result["strategy_universe_combinations"], 4)
        self.assertEqual(
            result["estimated_upper_bound_ledger_replays"],
            88,
        )
        get_price_data.assert_not_called()
        output_paths.assert_not_called()

    def test_combined_box_estimate_is_one_long_short_portfolio(self):
        csq_members = [f"T{index:03d}" for index in range(151)]
        us_members = (
            [f"T{index:03d}" for index in range(44)]
            + [f"U{index:03d}" for index in range(56)]
        )
        universe_groups = {
            "CSQ_UNIV": csq_members,
            "US_LARGE_CAP_100_20260630_PROXY": us_members,
        }
        training_config = (
            subject.portfolio_rl_overlay.EvolutionStrategyConfig(
                iterations=6,
                population=3,
            )
        )
        with (
            mock.patch.object(
                subject.config,
                "universe_groups",
                universe_groups,
            ),
            mock.patch.object(
                subject.core_market_data_store,
                "get_price_data",
            ) as get_price_data,
            mock.patch.object(subject, "_prepare_output_paths") as output_paths,
            mock.patch("builtins.print"),
        ):
            result = subject.run_experiment(
                groups=(
                    "CSQ_UNIV",
                    "US_LARGE_CAP_100_20260630_PROXY",
                ),
                combine_groups=True,
                combined_universe_name="CSQ_US100",
                strategies=("BOX",),
                seeds=(11, 23, 37),
                training_config=training_config,
                estimate_only=True,
            )

        self.assertEqual(result["strategy_universe_combinations"], 1)
        self.assertEqual(
            result["estimated_upper_bound_ledger_replays"],
            139,
        )
        self.assertEqual(
            result["portfolio_universes"],
            {"CSQ_US100": 207},
        )
        self.assertEqual(
            result["position_mode"],
            "LONG_SHORT_NEUTRAL",
        )
        get_price_data.assert_not_called()
        output_paths.assert_not_called()

    def test_latest_cutoff_never_adopts_cache_rows_after_oos_end(self):
        oos_end = pd.Timestamp(subject.core_backtest_periods.oos_end())
        future_date = oos_end + pd.Timedelta(days=30)
        future_frame = pd.DataFrame(
            {"Close": [98.0, 99.0, 100.0]},
            index=pd.DatetimeIndex(
                [
                    oos_end,
                    oos_end + pd.Timedelta(days=1),
                    future_date,
                ]
            ),
        )
        fixed_new_york_now = pd.Timestamp(
            oos_end + pd.Timedelta(days=10, hours=19),
            tz="America/New_York",
        )
        with mock.patch.object(subject, "datetime") as current_time:
            current_time.now.return_value = fixed_new_york_now
            cutoff = subject._latest_cutoff(
                {"AAA": future_frame},
                future_frame,
            )

        self.assertEqual(cutoff, oos_end.normalize())
        self.assertLess(cutoff, future_date)

    def test_latest_cutoff_excludes_new_york_monday_before_market_close(self):
        monday = pd.Timestamp("2026-07-27")
        thursday = monday - pd.Timedelta(days=4)
        friday = monday - pd.Timedelta(days=3)
        monday_frame = pd.DataFrame(
            {"Close": [98.0, 99.0, 100.0]},
            index=pd.DatetimeIndex([thursday, friday, monday]),
        )
        monday_morning = pd.Timestamp(
            "2026-07-27 11:00",
            tz="America/New_York",
        )

        with mock.patch.object(subject, "datetime") as current_time:
            current_time.now.return_value = monday_morning
            cutoff = subject._latest_cutoff(
                {"AAA": monday_frame},
                monday_frame,
            )

        self.assertEqual(cutoff, thursday)
        self.assertLess(cutoff, monday)

    def test_latest_cutoff_rejects_persisted_partial_session_after_close(self):
        thursday = pd.Timestamp("2026-07-23")
        friday = pd.Timestamp("2026-07-24")
        monday = pd.Timestamp("2026-07-27")
        cached_frame = pd.DataFrame(
            {"Close": [98.0, 99.0, 100.0]},
            index=pd.DatetimeIndex([thursday, friday, monday]),
        )
        monday_evening = pd.Timestamp(
            "2026-07-27 19:00",
            tz="America/New_York",
        )

        with mock.patch.object(subject, "datetime") as current_time:
            current_time.now.return_value = monday_evening
            cutoff = subject._latest_cutoff(
                {"AAA": cached_frame},
                cached_frame,
            )

        self.assertEqual(cutoff, friday)
        self.assertLess(cutoff, monday)

    def test_each_ticker_drops_its_latest_clock_safe_session(self):
        thursday = pd.Timestamp("2026-07-23")
        friday = pd.Timestamp("2026-07-24")
        monday = pd.Timestamp("2026-07-27")
        tuesday = pd.Timestamp("2026-07-28")
        price_data = {
            "STALE": pd.DataFrame(
                {"Close": [98.0, 99.0, 100.0]},
                index=pd.DatetimeIndex([thursday, friday, monday]),
            ),
            "CURRENT": pd.DataFrame(
                {"Close": [99.0, 100.0]},
                index=pd.DatetimeIndex([monday, tuesday]),
            ),
        }

        guarded, removed = (
            subject._drop_latest_clock_safe_session_per_ticker(
                price_data,
                tuesday,
            )
        )

        self.assertEqual(
            list(guarded["STALE"].index),
            [thursday, friday],
        )
        self.assertEqual(list(guarded["CURRENT"].index), [monday])
        self.assertEqual(removed["STALE"]["date"], "2026-07-27")
        self.assertEqual(removed["CURRENT"]["date"], "2026-07-28")
        self.assertIn(monday, price_data["STALE"].index)
        self.assertIn(tuesday, price_data["CURRENT"].index)

    def test_summary_uses_strict_positive_acwi_target(self):
        cases = (
            (5.01, 5.0, "PASS", True),
            (5.0, 5.0, "FAIL", False),
            (5.0, 0.0, "REVIEW", None),
        )
        for strategy_roi, acwi_roi, status, passes in cases:
            with self.subTest(
                strategy_roi=strategy_roi,
                acwi_roi=acwi_roi,
            ):
                row = subject._summary_row(
                    "Momentum",
                    "CSQ_UNIV",
                    "IS",
                    "BASELINE",
                    {"period": "2024-01-01 to 2024-12-31"},
                    _metrics(strategy_roi, acwi_roi),
                )
                self.assertEqual(row["acwi_target_status"], status)
                self.assertIs(
                    row["strictly_beats_positive_acwi"],
                    passes,
                )

    def test_custom_cvar_confidence_reaches_evaluate_and_generic_summary(self):
        performance = {"period": "2024-01-01 to 2024-12-31"}
        metrics = _metrics(6.0, 5.0)
        portfolio_policy = mock.Mock()
        with (
            mock.patch.object(
                subject.portfolio_rl_overlay,
                "evaluate_policy",
                return_value=performance,
            ),
            mock.patch.object(
                subject.portfolio_rl_overlay,
                "policy_metrics",
                return_value=metrics,
            ) as policy_metrics,
        ):
            actual_performance, actual_metrics = subject._evaluate(
                [],
                ["AAA"],
                {
                    "evaluation_start": "2024-01-01",
                    "evaluation_end": "2024-12-31",
                },
                mock.Mock(),
                mock.Mock(),
                portfolio_policy,
                cvar_confidence=0.90,
            )

        self.assertIs(actual_performance, performance)
        self.assertIs(actual_metrics, metrics)
        policy_metrics.assert_called_once_with(
            performance,
            confidence=0.90,
        )

        row = subject._summary_row(
            "Momentum",
            "CSQ_UNIV",
            "IS",
            "BASELINE",
            performance,
            metrics,
            cvar_confidence=0.90,
        )
        self.assertEqual(row["cvar_confidence"], 0.90)
        self.assertEqual(row["portfolio_cvar_pct"], 1.0)
        self.assertNotIn("portfolio_cvar95_pct", row)

    def test_evaluate_changes_only_sizing_policy_between_baseline_and_rl(self):
        performance = {"period": "2024-01-01 to 2024-12-31"}
        metrics = _metrics(6.0, 5.0)
        price_loader = mock.Mock()
        market_loader = mock.Mock()
        portfolio_policy = mock.Mock()
        rl_policy = mock.Mock()
        window = {
            "evaluation_start": "2024-01-01",
            "evaluation_end": "2024-12-31",
        }
        with (
            mock.patch.object(
                subject.portfolio_rl_overlay,
                "evaluate_policy",
                return_value=performance,
            ) as evaluate_policy,
            mock.patch.object(
                subject.portfolio_rl_overlay,
                "policy_metrics",
                return_value=metrics,
            ),
        ):
            subject._evaluate(
                [],
                ["AAA"],
                window,
                price_loader,
                market_loader,
                portfolio_policy,
            )
            baseline_kwargs = evaluate_policy.call_args.kwargs
            self.assertIsNone(baseline_kwargs["sizing_policy"])
            self.assertIs(
                baseline_kwargs["portfolio_policy"],
                portfolio_policy,
            )

            evaluate_policy.reset_mock()
            subject._evaluate(
                [],
                ["AAA"],
                window,
                price_loader,
                market_loader,
                portfolio_policy,
                policy=rl_policy,
            )
            rl_kwargs = evaluate_policy.call_args.kwargs
            self.assertIs(rl_kwargs["sizing_policy"], rl_policy)
            self.assertIs(
                rl_kwargs["portfolio_policy"],
                portfolio_policy,
            )


if __name__ == "__main__":
    unittest.main()
