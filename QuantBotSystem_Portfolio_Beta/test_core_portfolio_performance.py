import unittest

import pandas as pd

import core_portfolio_performance


def _loader(frames):
    def load(ticker):
        return frames.get(ticker, pd.DataFrame())
    return load


def _frame(closes, dates=None):
    index = pd.to_datetime(dates or ["2025-01-01", "2025-01-02", "2025-01-03"])
    return pd.DataFrame({"Close": closes}, index=index)


class PortfolioPerformanceTests(unittest.TestCase):
    def test_next_open_entry_and_close_exit_compound_in_daily_nav(self):
        trades = [{
            "ticker": "AAA", "side": "LONG", "entry_date": "2025-01-02",
            "entry_price": 100.0, "exit_date": "2025-01-03", "exit_price": 121.0,
        }]
        result = core_portfolio_performance.calculate_equal_weight_performance(
            trades, ["AAA"], "2025-01-02", "2025-01-03",
            price_loader=_loader({"AAA": _frame([100.0, 110.0, 121.0])}),
        )
        self.assertAlmostEqual(result["daily"]["strategy_return"].iloc[0], 0.10)
        self.assertAlmostEqual(result["daily"]["strategy_return"].iloc[1], 0.10)
        self.assertAlmostEqual(result["strategy"]["roi_pct"], 21.0)

    def test_short_direction_is_reflected(self):
        trades = [{
            "ticker": "AAA", "side": "SHORT", "entry_date": "2025-01-02",
            "entry_price": 100.0, "exit_date": "2025-01-02", "exit_price": 90.0,
        }]
        result = core_portfolio_performance.calculate_equal_weight_performance(
            trades, ["AAA"], "2025-01-02", "2025-01-02",
            price_loader=_loader({"AAA": _frame([100.0, 90.0, 90.0])}),
        )
        self.assertAlmostEqual(result["strategy"]["roi_pct"], 10.0)

    def test_partial_exit_cash_stays_in_trade_sleeve(self):
        trades = [{
            "ticker": "AAA", "side": "LONG", "entry_date": "2025-01-02",
            "entry_price": 100.0,
            "exit_events": [
                {"date": "2025-01-02", "weight": 0.5, "price": 110.0},
                {"date": "2025-01-03", "weight": 0.5, "price": 120.0},
            ],
        }]
        result = core_portfolio_performance.calculate_equal_weight_performance(
            trades, ["AAA"], "2025-01-02", "2025-01-03",
            price_loader=_loader({"AAA": _frame([100.0, 110.0, 120.0])}),
        )
        self.assertAlmostEqual(result["daily"]["strategy_nav"].iloc[0], 1.10)
        self.assertAlmostEqual(result["daily"]["strategy_nav"].iloc[1], 1.15)

    def test_active_trade_sleeves_are_equal_weighted_each_day(self):
        trades = [
            {"ticker": "AAA", "side": "LONG", "entry_date": "2025-01-02",
             "entry_price": 100.0, "exit_date": "2025-01-02", "exit_price": 110.0},
            {"ticker": "BBB", "side": "LONG", "entry_date": "2025-01-02",
             "entry_price": 100.0, "exit_date": "2025-01-02", "exit_price": 90.0},
        ]
        frames = {"AAA": _frame([100.0, 110.0, 110.0]),
                  "BBB": _frame([100.0, 90.0, 90.0])}
        result = core_portfolio_performance.calculate_equal_weight_performance(
            trades, ["AAA", "BBB"], "2025-01-02", "2025-01-02",
            price_loader=_loader(frames),
        )
        self.assertAlmostEqual(result["strategy"]["roi_pct"], 0.0)
        self.assertEqual(result["daily"]["active_positions"].iloc[0], 2)

    def test_first_day_loss_is_included_in_mdd(self):
        trades = [{
            "ticker": "AAA", "side": "LONG", "entry_date": "2025-01-02",
            "entry_price": 100.0, "exit_date": "2025-01-02", "exit_price": 90.0,
        }]
        result = core_portfolio_performance.calculate_equal_weight_performance(
            trades, ["AAA"], "2025-01-02", "2025-01-02",
            price_loader=_loader({"AAA": _frame([100.0, 90.0, 90.0])}),
        )
        self.assertAlmostEqual(result["strategy"]["mdd_pct"], -10.0)

    def test_benchmark_equal_weights_and_forward_fills_market_holiday(self):
        frames = {
            "AAA": _frame([100.0, 110.0, 121.0]),
            "BBB": _frame(
                [200.0, 220.0], dates=["2025-01-01", "2025-01-03"]
            ),
        }
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [], ["AAA", "BBB"], "2025-01-02", "2025-01-03",
            price_loader=_loader(frames),
        )
        returns = result["daily"]["benchmark_return"]
        self.assertAlmostEqual(returns.iloc[0], 0.05)
        self.assertAlmostEqual(returns.iloc[1], 0.10)
        self.assertEqual(result["benchmark_tickers"], 2)

    def test_benchmark_seeds_each_ticker_from_its_own_last_prior_date(self):
        frames = {
            "AAA": _frame(
                [100.0, 110.0],
                dates=["2025-01-03", "2025-01-06"],
            ),
            "BBB": _frame(
                [200.0, 220.0],
                dates=["2025-01-02", "2025-01-06"],
            ),
        }

        result = core_portfolio_performance.calculate_equal_weight_performance(
            [],
            ["AAA", "BBB"],
            "2025-01-06",
            "2025-01-06",
            price_loader=_loader(frames),
        )

        self.assertAlmostEqual(
            result["daily"]["benchmark_return"].iloc[0],
            0.10,
        )
        self.assertEqual(
            result["daily"]["benchmark_constituents"].iloc[0],
            2,
        )

    def test_stale_benchmark_price_is_not_forward_filled_indefinitely(self):
        dates = ["2025-01-01", "2025-01-02", "2025-01-10"]
        frames = {
            "AAA": _frame([100.0, 110.0, 121.0], dates=dates),
            "BBB": _frame([200.0], dates=["2025-01-01"]),
        }
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [], ["AAA", "BBB"], "2025-01-02", "2025-01-10",
            price_loader=_loader(frames), max_stale_calendar_days=7,
        )
        self.assertEqual(result["daily"]["benchmark_constituents"].iloc[-1], 1)
        self.assertEqual(result["benchmark_min_daily_tickers"], 1)

    def test_msci_acwi_is_primary_and_universe_ew_remains_separate(self):
        frames = {
            "AAA": _frame([100.0, 110.0, 121.0]),
            "BBB": _frame([100.0, 100.0, 100.0]),
            "MIWD00000PUS": _frame([100.0, 110.0, 121.0]),
        }
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [],
            ["AAA", "BBB"],
            "2025-01-02",
            "2025-01-03",
            price_loader=_loader(frames),
        )

        self.assertAlmostEqual(result["market_benchmark"]["roi_pct"], 21.0)
        self.assertAlmostEqual(result["universe_ew_benchmark"]["roi_pct"], 10.25)
        self.assertAlmostEqual(result["benchmark"]["roi_pct"], 10.25)
        self.assertAlmostEqual(
            result["daily"]["market_benchmark_return"].iloc[0],
            0.10,
        )
        self.assertAlmostEqual(result["daily"]["universe_ew_return"].iloc[0], 0.05)
        self.assertEqual(result["market_benchmark_ticker"], "MIWD00000PUS")
        self.assertTrue(result["market_benchmark_available"])

    def test_missing_msci_acwi_is_not_silently_replaced_by_universe_ew(self):
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [],
            ["AAA"],
            "2025-01-02",
            "2025-01-03",
            price_loader=_loader({"AAA": _frame([100.0, 110.0, 121.0])}),
        )

        self.assertFalse(result["market_benchmark_available"])
        self.assertIsNone(result["market_benchmark"]["roi_pct"])
        self.assertTrue(result["daily"]["market_benchmark_return"].isna().all())
        self.assertAlmostEqual(result["universe_ew_benchmark"]["roi_pct"], 21.0)
        self.assertIn("MIWD00000PUS", result["market_benchmark_unavailable_reason"])

    def test_msci_acwi_roi_uses_boundary_levels_without_full_daily_history(self):
        dates = ["2025-01-02", "2025-01-15", "2025-01-31"]
        frames = {
            "AAA": _frame([100.0, 101.0, 102.0], dates=dates),
            "MIWD00000PUS": _frame(
                [100.0, 110.0, 999.0],
                dates=["2024-12-31", "2025-01-31", "2025-02-01"],
            ),
        }
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [],
            ["AAA"],
            "2025-01-02",
            "2025-01-31",
            price_loader=_loader(frames),
        )

        self.assertTrue(result["market_benchmark_available"])
        self.assertAlmostEqual(result["market_benchmark"]["roi_pct"], 10.0)
        self.assertEqual(
            result["market_benchmark_start_level_date"],
            "2024-12-31",
        )
        self.assertEqual(result["market_benchmark_end_level_date"], "2025-01-31")
        self.assertEqual(result["market_benchmark_end_level"], 110.0)
        self.assertFalse(result["market_benchmark_daily_curve_available"])
        self.assertTrue(result["daily"]["market_benchmark_nav"].isna().all())

    def test_acwi_move_on_universe_holiday_carries_to_next_evaluation_date(self):
        frames = {
            "AAA": _frame(
                [100.0, 101.0],
                dates=["2025-01-02", "2025-01-04"],
            ),
            "MIWD00000PUS": _frame(
                [100.0, 110.0, 121.0],
                dates=["2024-12-31", "2025-01-03", "2025-01-04"],
            ),
        }
        result = core_portfolio_performance.calculate_equal_weight_performance(
            [],
            ["AAA"],
            "2025-01-02",
            "2025-01-04",
            price_loader=_loader(frames),
        )

        self.assertTrue(result["market_benchmark_daily_curve_available"])
        self.assertAlmostEqual(result["market_benchmark"]["roi_pct"], 21.0)
        self.assertAlmostEqual(
            result["daily"]["market_benchmark_return"].iloc[-1],
            0.21,
        )
        self.assertAlmostEqual(
            result["daily"]["market_benchmark_nav"].iloc[-1],
            1.21,
        )

    def test_universe_result_adds_trade_stats_and_target_decision(self):
        universe_result = {
            "period": "2025-01-02 to 2025-01-03",
            "trades": [{
                "ticker": "AAA", "side": "LONG", "entry_date": "2025-01-02",
                "entry_price": 100.0, "exit_date": "2025-01-03", "exit_price": 121.0,
            }],
            "all": {"count": 1, "win_rate": 50.0},
        }
        result = core_portfolio_performance.calculate_universe_performance(
            universe_result, ["AAA", "BBB"],
            price_loader=_loader({
                "AAA": _frame([100.0, 110.0, 121.0]),
                "BBB": _frame([100.0, 100.0, 100.0]),
                "MIWD00000PUS": _frame([100.0, 100.2, 100.4]),
            }),
        )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_rate_pct"], 50.0)
        self.assertFalse(result["target"]["passes_win_rate"])
        # A 21% trade return on a capped 5% position contributes 1.05% to NAV.
        # It beats twice the +0.4% ACWI move even though it trails Universe EW.
        self.assertAlmostEqual(result["strategy"]["roi_pct"], 1.05)
        self.assertTrue(result["target"]["passes_all"])
        self.assertAlmostEqual(result["market_benchmark"]["roi_pct"], 0.4, places=2)
        self.assertGreater(result["universe_ew_benchmark"]["roi_pct"], 10.0)
        self.assertEqual(result["target"]["benchmark_ticker"], "MIWD00000PUS")
        self.assertEqual(result["target"]["primary_metric"], "1Y cumulative ROI")
        self.assertFalse(result["target"]["win_rate_is_hard_gate"])
        self.assertIn("passes_all", result["target"])

    def test_return_target_uses_roi_and_win_rate_is_secondary(self):
        passed = core_portfolio_performance.evaluate_return_target(22.0, 10.0, 50.0)
        self.assertEqual(passed["status"], "PASS")
        self.assertTrue(passed["passes_return_target"])
        self.assertFalse(passed["meets_win_rate_goal"])

        failed = core_portfolio_performance.evaluate_return_target(19.0, 10.0, 70.0)
        self.assertEqual(failed["status"], "FAIL")
        self.assertFalse(failed["passes_return_target"])
        self.assertTrue(failed["meets_win_rate_goal"])

        review = core_portfolio_performance.evaluate_return_target(5.0, -2.0, 70.0)
        self.assertEqual(review["status"], "REVIEW")
        self.assertIsNone(review["passes_return_target"])


if __name__ == "__main__":
    unittest.main()
