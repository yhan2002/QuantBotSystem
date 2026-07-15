import unittest

import core_universe_backtest


class UniverseBacktestTests(unittest.TestCase):
    def test_aggregates_every_ticker_and_splits_summary_detail(self):
        calls = []

        def runner(ticker, include_trades=False, **kwargs):
            calls.append((ticker, include_trades, kwargs.get("evaluation_days")))
            side = "LONG" if ticker == "AAA" else "SHORT"
            value = 2.0 if ticker == "AAA" else -1.0
            details = [
                {
                    "side": side,
                    "entry_date": "2025-08-01",
                    "exit_date": "2025-08-08",
                    "return": value,
                }
            ]
            return value, 100.0 if value > 0 else 0.0, 1, "2025-07-15 to 2026-07-15", details

        result = core_universe_backtest.aggregate_universe_backtest(
            ["AAA", "BBB"], runner, evaluation_days=252
        )
        report = core_universe_backtest.format_universe_backtest(
            result, "entry rule", "exit rule"
        )
        summary, detail = core_universe_backtest.split_summary_detail(report)

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(include_trades for _, include_trades, _ in calls))
        self.assertEqual(result["all"]["count"], 2)
        self.assertIn("Entry: entry rule", summary)
        self.assertIn("Exit: exit rule", summary)
        self.assertIn("ALL: trades 2", summary)
        self.assertNotIn("2025-08-01", summary)
        self.assertIn("LONG AAA: 2025-08-01", detail)
        self.assertIn("SHORT BBB: 2025-08-01", detail)


if __name__ == "__main__":
    unittest.main()
