import unittest

import analyze_strategy_universe_direction as subject


class StrategyUniverseDirectionAnalysisTests(unittest.TestCase):
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
        passed = subject.target_status(40, 50.0, 22.0, 10.0)
        self.assertEqual(passed["status"], "PASS")
        self.assertTrue(passed["passes_2x_benchmark_roi"])
        self.assertFalse(passed["passes_58pct_win_rate"])

        roi_missed = subject.target_status(40, 70.0, 19.0, 10.0)
        self.assertEqual(roi_missed["status"], "FAIL")
        self.assertFalse(roi_missed["passes_2x_benchmark_roi"])
        self.assertTrue(roi_missed["passes_58pct_win_rate"])

        low_sample = subject.target_status(29, 70.0, 30.0, 10.0)
        self.assertEqual(low_sample["status"], "INSUFFICIENT_TRADES")

        negative_benchmark = subject.target_status(40, 70.0, 5.0, -2.0)
        self.assertEqual(negative_benchmark["status"], "REVIEW")
        self.assertIsNone(negative_benchmark["passes_2x_benchmark_roi"])

    def test_primary_universe_is_deduplicated(self):
        specs = subject._universe_specs()
        expected = sorted(set(specs["CSQ"]) | set(specs["JP"]) | set(specs["CH"]))
        self.assertEqual(specs["Primary Combined"], expected)


if __name__ == "__main__":
    unittest.main()
