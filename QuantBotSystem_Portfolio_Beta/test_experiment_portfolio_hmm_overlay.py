import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

import pandas as pd

import experiment_portfolio_hmm_overlay as subject


class HMMExperimentTests(unittest.TestCase):
    @patch.object(subject.core_market_data_store, "get_price_data")
    def test_estimate_only_never_reads_market_data(self, get_price_data):
        result = subject.run_experiment(group="CSQ_UNIV", estimate_only=True)
        get_price_data.assert_not_called()
        self.assertEqual(result["external_downloads"], 0)
        self.assertEqual(result["portfolio_replays"], 6)
        self.assertEqual(
            result["strategy_scans"],
            result["universe_count"] * 3 * 2,
        )

    @patch.object(subject.core_market_data_store, "get_price_data")
    def test_long_run_requires_explicit_confirmation(self, get_price_data):
        with self.assertRaisesRegex(RuntimeError, "--estimate-only"):
            subject.run_experiment(group="CSQ_UNIV")
        get_price_data.assert_not_called()

    def test_unknown_group_fails_before_work(self):
        with self.assertRaisesRegex(KeyError, "Unknown universe group"):
            subject.run_experiment(group="NOT_A_GROUP", estimate_only=True)

    def test_existing_output_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                subject._write_output(path, {"new": True})
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_universe_features_prefer_adjusted_close(self):
        dates = pd.date_range("2026-01-01", periods=2)
        price_data = {
            "AAA": pd.DataFrame(
                {"Close": [100.0, 50.0], "Adj Close": [50.0, 50.0]},
                index=dates,
            ),
            "BBB": pd.DataFrame({"Close": [20.0, 21.0]}, index=dates),
        }
        result = subject._universe_close(price_data, ["AAA", "BBB"])
        self.assertEqual(result["AAA"].tolist(), [50.0, 50.0])
        self.assertEqual(result["BBB"].tolist(), [20.0, 21.0])

    def test_hmm_metrics_report_turnover_and_strategy_capital_ratios(self):
        performance = {
            "ledger": {
                "trades": [
                    {
                        "strategy": "Momentum",
                        "base_requested_weight": 0.05,
                        "requested_weight": 0.04,
                    },
                    {
                        "strategy": "Box Range",
                        "base_requested_weight": 0.01,
                        "requested_weight": 0.02,
                    },
                ]
            }
        }
        metrics = subject._routing_metrics(performance)
        self.assertAlmostEqual(metrics["capital_preservation_ratio"], 1.0)
        self.assertAlmostEqual(metrics["redistribution_turnover_pct"], 100 / 6)
        self.assertAlmostEqual(metrics["strategy_capital_ratios"]["Momentum"], 0.8)
        self.assertAlmostEqual(metrics["strategy_capital_ratios"]["Box Range"], 2.0)

    def test_state_statistics_report_duration_and_transitions(self):
        dates = pd.date_range("2026-01-01", periods=5)
        probability = pd.Series([0.8, 0.7, 0.2, 0.1, 0.5], index=dates)
        result = subject._state_statistics(
            probability,
            {
                "evaluation_start": "2026-01-01",
                "evaluation_end": "2026-01-05",
            },
        )
        self.assertEqual(result["transitions"], 2)
        self.assertEqual(result["average_duration_sessions"]["TREND"], 2.0)
        self.assertEqual(result["average_duration_sessions"]["NON_TREND"], 2.0)
        self.assertEqual(result["average_duration_sessions"]["NEUTRAL"], 1.0)


if __name__ == "__main__":
    unittest.main()
