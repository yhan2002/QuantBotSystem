import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

import analyze_strategy_universe_direction as subject


class StrategyUniverseDirectionAnalysisTests(unittest.TestCase):
    def test_new_box_runner_uses_shared_next_open_contract(self):
        prices = pd.DataFrame()
        runner = Mock(return_value=(0.0, 0.0, 0, "N/A", []))

        with patch.object(subject.strategy_box_range_new, "run_box_backtest", runner):
            result = subject._runner(
                "Box Range (New one)",
                "AAA",
                prices,
                "LONG",
                {"start_date": "2026-01-01", "end_date": "2026-07-31"},
            )

        self.assertEqual((0.0, 0.0, 0, "N/A", []), result)
        runner.assert_called_once_with(
            "AAA",
            side_filter="LONG",
            period=subject.core_backtest_periods.HISTORY_PERIOD,
            include_trades=True,
            df=prices,
            entry_execution=subject.core_entry_execution.NEXT_OPEN,
            start_date="2026-01-01",
            end_date="2026-07-31",
        )

    def test_trade_stats(self):
        stats = subject.trade_stats(
            [{"return": 10.0}, {"return": -4.0}, {"return": 2.0}]
        )
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["wins"], 2)
        self.assertAlmostEqual(stats["avg"], 8.0 / 3.0)
        self.assertEqual(stats["median"], 2.0)
        self.assertAlmostEqual(stats["win_rate"], 200.0 / 3.0)

    def test_aggregate_cached_results_preserves_sides_and_last_exit(self):
        cached = {
            "AAA": (
                1.0,
                100.0,
                1,
                "2026-01-01 to 2026-07-17",
                [
                    {
                        "side": "LONG",
                        "entry_date": "2026-01-05",
                        "return": 3.0,
                        "exit_events": [
                            {"date": "2026-01-08", "weight": 0.5, "price": 103.0},
                            {"date": "2026-01-10", "weight": 0.5, "price": 103.0},
                        ],
                    }
                ],
            ),
            "BBB": (
                -1.0,
                0.0,
                1,
                "2026-01-02 to 2026-07-16",
                [
                    {
                        "side": "SHORT",
                        "entry_date": "2026-02-01",
                        "exit_date": "2026-02-03",
                        "return": -1.0,
                    }
                ],
            ),
            "CCC": (0.0, 0.0, 0, "N/A", []),
        }
        result = subject.aggregate_cached_results(["AAA", "BBB", "CCC"], cached)
        self.assertEqual(result["eligible_tickers"], 2)
        self.assertEqual(result["failed_tickers"], ["CCC"])
        self.assertEqual(result["all"]["count"], 2)
        self.assertEqual(result["long"]["count"], 1)
        self.assertEqual(result["short"]["count"], 1)
        self.assertEqual(result["trades"][0]["ticker"], "AAA")
        self.assertEqual(result["trades"][0]["exit_date"], "2026-01-10")
        self.assertEqual(result["period"], "2026-01-01 to 2026-07-17")

    def test_target_status_requires_sample_and_roi_while_win_is_secondary(self):
        passed = subject.target_status(40, 50.0, 10.01, 10.0)
        self.assertEqual(passed["status"], "PASS")
        self.assertTrue(passed["passes_acwi_roi"])
        self.assertTrue(passed["passes_benchmark_roi"])
        self.assertFalse(passed["passes_58pct_win_rate"])

        roi_missed = subject.target_status(40, 70.0, 10.0, 10.0)
        self.assertEqual(roi_missed["status"], "FAIL")
        self.assertFalse(roi_missed["passes_acwi_roi"])
        self.assertTrue(roi_missed["passes_58pct_win_rate"])

        low_sample = subject.target_status(29, 70.0, 30.0, 10.0)
        self.assertEqual(low_sample["status"], "INSUFFICIENT_TRADES")

        negative_benchmark = subject.target_status(40, 70.0, 5.0, -2.0)
        self.assertEqual(negative_benchmark["status"], "REVIEW")
        self.assertIsNone(negative_benchmark["passes_benchmark_roi"])

        nonpositive_low_sample = subject.target_status(1, 70.0, 5.0, 0.0)
        self.assertEqual(nonpositive_low_sample["status"], "REVIEW")

    def test_target_status_uses_acwi_and_keeps_universe_ew_diagnostic_only(self):
        target = subject.target_status(
            40,
            50.0,
            11.0,
            10.0,
            universe_ew_roi_pct=12.0,
        )
        self.assertEqual(target["status"], "PASS")
        self.assertTrue(target["passes_acwi_roi"])
        self.assertTrue(target["passes_benchmark_roi"])

        missing_acwi = subject.target_status(
            10,
            70.0,
            11.0,
            None,
            universe_ew_roi_pct=10.0,
        )
        self.assertEqual(missing_acwi["status"], "REVIEW")
        self.assertIsNone(missing_acwi["passes_acwi_roi"])
        self.assertIsNone(missing_acwi["passes_benchmark_roi"])

    def test_performance_row_maps_acwi_and_universe_ew_contract(self):
        index = pd.DatetimeIndex(["2026-01-02"])
        daily = pd.DataFrame(
            {
                "strategy_return": [0.02],
                "strategy_nav": [1.02],
                "market_benchmark_return": [0.01],
                "market_benchmark_nav": [1.01],
                "universe_ew_return": [0.015],
                "universe_ew_nav": [1.015],
                "universe_ew_constituents": [2],
                "active_positions": [1],
                "cash": [50.0],
                "long_market_value": [50.0],
                "short_market_value": [0.0],
                "gross_exposure_pct": [50.0],
                "net_exposure_pct": [50.0],
            },
            index=index,
        )
        performance = {
            "daily": daily,
            "strategy": {
                "roi_pct": 11.0,
                "cagr_pct": 20.0,
                "mdd_pct": -5.0,
                "sharpe": 1.2,
            },
            "market_benchmark": {
                "roi_pct": 10.0,
                "cagr_pct": 9.0,
                "mdd_pct": -8.0,
                "sharpe": 0.8,
            },
            "universe_ew_benchmark": {
                "roi_pct": 12.0,
                "cagr_pct": 11.0,
                "mdd_pct": -7.0,
                "sharpe": 0.9,
            },
            "market_benchmark_available": True,
            "market_benchmark_unavailable_reason": "",
            "market_benchmark_coverage_pct": 100.0,
            "market_benchmark_multiple": 1.1,
            "market_excess_roi_pct": 1.0,
            "market_excess_cagr_pct": 11.0,
            "stock_selection_excess_pct": -1.0,
            "excess_cagr_pct": 9.0,
            "universe_ew_tickers": 2,
            "universe_ew_min_daily_tickers": 2,
            "universe_ew_max_daily_tickers": 2,
            "ledger": {
                "trades": [
                    {
                        "ticker": "AAA",
                        "side": "LONG",
                        "return": 3.0,
                        "allocation_status": "FILLED",
                    }
                ]
            },
        }
        loader = Mock()
        rows = []
        result = {
            "period": "2026-01-01 to 2026-01-02",
            "trades": [],
            "universe_count": 2,
            "eligible_tickers": 2,
            "failed_tickers": [],
        }
        with patch.object(
            subject.core_portfolio_performance,
            "calculate_score_weighted_performance",
            return_value=performance,
        ) as calculate:
            row = subject._performance_row(
                "Momentum",
                "CSQ",
                "LONG",
                "OOS",
                result,
                ["AAA", "BBB"],
                {"AAA": pd.DataFrame(), "BBB": pd.DataFrame()},
                rows,
                15,
                1,
                market_benchmark_loader=loader,
            )

        self.assertIs(calculate.call_args.kwargs["market_benchmark_loader"], loader)
        self.assertEqual(row["benchmark_roi_pct"], 12.0)
        self.assertEqual(row["universe_ew_roi_pct"], 12.0)
        self.assertEqual(row["msci_acwi_roi_pct"], 10.0)
        self.assertEqual(row["msci_acwi_multiple"], 1.1)
        self.assertTrue(row["passes_acwi_roi"])
        self.assertTrue(row["passes_benchmark_roi"])
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(rows[0]["benchmark_return_pct"], 1.5)
        self.assertEqual(rows[0]["universe_ew_return_pct"], 1.5)
        self.assertEqual(rows[0]["msci_acwi_return_pct"], 1.0)

    def test_performance_row_keeps_missing_acwi_numeric_fields_blank_and_review(self):
        index = pd.DatetimeIndex(["2026-01-02"])
        daily = pd.DataFrame(
            {
                "strategy_return": [0.02],
                "strategy_nav": [1.02],
                "market_benchmark_return": [float("nan")],
                "market_benchmark_nav": [float("nan")],
                "universe_ew_return": [0.015],
                "universe_ew_nav": [1.015],
                "universe_ew_constituents": [1],
                "active_positions": [0],
                "cash": [100.0],
                "long_market_value": [0.0],
                "short_market_value": [0.0],
                "gross_exposure_pct": [0.0],
                "net_exposure_pct": [0.0],
            },
            index=index,
        )
        performance = {
            "daily": daily,
            "strategy": {
                "roi_pct": 5.0,
                "cagr_pct": 5.0,
                "mdd_pct": 0.0,
                "sharpe": None,
            },
            "market_benchmark": {
                "roi_pct": None,
                "cagr_pct": None,
                "mdd_pct": None,
                "sharpe": None,
            },
            "universe_ew_benchmark": {
                "roi_pct": 2.0,
                "cagr_pct": 2.0,
                "mdd_pct": -1.0,
                "sharpe": 0.5,
            },
            "market_benchmark_available": False,
            "market_benchmark_unavailable_reason": "No local ACWI data",
            "market_benchmark_coverage_pct": 0.0,
            "market_benchmark_multiple": None,
            "market_excess_roi_pct": None,
            "market_excess_cagr_pct": None,
            "stock_selection_excess_pct": 3.0,
            "excess_cagr_pct": 3.0,
            "universe_ew_tickers": 1,
            "universe_ew_min_daily_tickers": 1,
            "universe_ew_max_daily_tickers": 1,
            "ledger": {"trades": []},
        }
        rows = []
        result = {
            "period": "2026-01-01 to 2026-01-02",
            "trades": [],
            "universe_count": 1,
            "eligible_tickers": 1,
            "failed_tickers": [],
        }
        with patch.object(
            subject.core_portfolio_performance,
            "calculate_score_weighted_performance",
            return_value=performance,
        ):
            row = subject._performance_row(
                "Momentum",
                "CSQ",
                "LONG",
                "OOS",
                result,
                ["AAA"],
                {"AAA": pd.DataFrame()},
                rows,
                15,
                30,
            )

        self.assertEqual(row["status"], "REVIEW")
        self.assertEqual(row["error"], "")
        self.assertIsNone(row["msci_acwi_roi_pct"])
        self.assertEqual(row["msci_acwi_coverage_pct"], "")
        self.assertEqual(rows[0]["msci_acwi_return_pct"], "")
        self.assertEqual(rows[0]["msci_acwi_nav"], "")
        self.assertEqual(rows[0]["benchmark_return_pct"], 1.5)

    def test_primary_universe_is_deduplicated(self):
        specs = subject._universe_specs()
        expected = sorted(set(specs["CSQ"]) | set(specs["JP"]) | set(specs["CH"]))
        self.assertEqual(specs["Primary Combined"], expected)

    def test_run_analysis_loads_acwi_once_outside_trading_universe(self):
        prices = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "Volume": [1000.0],
            },
            index=pd.DatetimeIndex(["2026-07-01"]),
        )
        price_loader = Mock(side_effect=lambda ticker, **_: prices.copy())
        performance_row = Mock(
            return_value={
                "strategy": "Momentum",
                "universe": "CSQ",
                "mode": "LONG",
                "window": "OOS",
                "trade_count": 0,
                "strategy_roi_pct": 0.0,
                "win_rate_pct": 0.0,
                "msci_acwi_multiple": None,
                "stock_selection_excess_pct": 0.0,
                "status": "REVIEW",
            }
        )
        runner = Mock(return_value=(0.0, 0.0, 0, "N/A", []))
        with (
            TemporaryDirectory() as output_dir,
            patch.object(subject, "BASE_GROUPS", {"CSQ": "unused"}),
            patch.object(subject, "UNIVERSE_ORDER", ("CSQ",)),
            patch.object(subject, "DIRECTIONAL_STRATEGIES", ("Momentum",)),
            patch.object(subject, "WINDOW_ORDER", ("OOS",)),
            patch.object(subject, "MODE_SIDES", {"LONG": "LONG"}),
            patch.object(subject, "_universe_specs", return_value={"CSQ": ["AAA"]}),
            patch.object(subject, "_runner", runner),
            patch.object(subject, "_performance_row", performance_row),
            patch.object(
                subject.core_market_data_store,
                "get_price_data",
                price_loader,
            ),
            patch("builtins.print"),
        ):
            _, _, metadata, *_ = subject.run_analysis(
                output_dir=output_dir,
                timestamp="test",
            )

        loaded_tickers = [call.args[0] for call in price_loader.call_args_list]
        self.assertEqual(loaded_tickers, ["AAA", subject.ACWI_TICKER])
        self.assertEqual({call.args[1] for call in runner.call_args_list}, {"AAA"})
        self.assertEqual(performance_row.call_args.args[5], ["AAA"])
        benchmark_loader = performance_row.call_args.kwargs[
            "market_benchmark_loader"
        ]
        pd.testing.assert_frame_equal(
            benchmark_loader(subject.ACWI_TICKER),
            prices,
        )
        self.assertEqual(metadata["schema_version"], subject.SCHEMA_VERSION)
        self.assertEqual(
            metadata["benchmark_roles"]["primary_pass_fail"]["ticker"],
            "ACWI",
        )
        self.assertEqual(
            metadata["benchmark_roles"]["stock_selection_diagnostic"]["label"],
            "Universe EW",
        )
        self.assertIn("strictly greater", metadata["assumptions"]["primary_target"])
        self.assertIn("passes_acwi_roi", subject.SUMMARY_COLUMNS)
        self.assertIn("passes_benchmark_roi", subject.SUMMARY_COLUMNS)
        self.assertNotIn("passes_2x_acwi_roi", subject.SUMMARY_COLUMNS)


if __name__ == "__main__":
    unittest.main()
