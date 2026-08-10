import math
import unittest
from unittest.mock import patch

import pandas as pd

import experiment_reversion_brain_alpha
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


def _brain_frames(count=20, periods=300):
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    frames = {}
    for ticker_index in range(count):
        close = [
            80.0
            + ticker_index * 1.7
            + day * (0.015 + ticker_index * 0.0003)
            + (1.0 + ticker_index * 0.04)
            * math.sin(day / (4.0 + ticker_index % 5) + ticker_index * 0.31)
            for day in range(periods)
        ]
        frames[f"T{ticker_index:02d}"] = pd.DataFrame(
            {"Close": close},
            index=dates,
        )
    return frames


class ValueReversionEngineTests(unittest.TestCase):
    @patch.object(
        experiment_reversion_brain_alpha.config,
        "universe_groups",
        {"TEST": ["AAA", "ACWI"]},
    )
    def test_brain_experiment_estimates_without_running_backtests(self):
        estimate = experiment_reversion_brain_alpha.run_experiment(
            group="TEST",
            estimate_only=True,
        )["estimate"]
        self.assertEqual(estimate["universe_count"], 1)
        self.assertEqual(estimate["ticker_backtests"], 6)
        self.assertEqual(estimate["external_downloads"], 0)
        with self.assertRaisesRegex(RuntimeError, "--confirm-long-run"):
            experiment_reversion_brain_alpha.run_experiment(group="TEST")

    def test_country_policy_keeps_us_baseline(self):
        exact = strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_EXACT
        unit_safe = strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_UNIT_SAFE
        for country in ("CH", "JP", "KR"):
            self.assertEqual(
                strategy_price_liquidity_reversion.brain_alpha_variant_for_country(
                    exact, country
                ),
                "EXACT",
            )
            self.assertEqual(
                strategy_price_liquidity_reversion.brain_alpha_variant_for_country(
                    unit_safe, country
                ),
                "UNIT_SAFE",
            )
        self.assertIsNone(
            strategy_price_liquidity_reversion.brain_alpha_variant_for_country(
                exact, "US"
            )
        )

    def test_policy_off_returns_baseline_rows_unchanged(self):
        rows = [{"ticker": "AAA", "side": "LONG", "signal_date": "2025-01-02"}]
        result = strategy_price_liquidity_reversion.apply_brain_alpha_policy(
            rows,
            {},
            {},
            strategy_price_liquidity_reversion.BRAIN_ALPHA_OFF,
        )
        self.assertIs(result, rows)

    def test_time_series_rank_uses_zero_to_one_average_ties(self):
        rank = strategy_price_liquidity_reversion._ts_rank_last(
            pd.Series([1.0, 2.0, 3.0, 2.0, 2.0]),
            5,
        )
        self.assertEqual(rank, 0.5)

    def test_alpha_direction_mismatch_rejects_and_missing_falls_back(self):
        policy = strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_UNIT_SAFE
        rows = [
            {"ticker": "ALLOW", "side": "LONG"},
            {"ticker": "REJECT", "side": "SHORT"},
            {"ticker": "ZERO", "side": "LONG"},
            {"ticker": "MISSING", "side": "SHORT"},
            {"ticker": "US", "side": "SHORT"},
        ]
        metadata = {
            ticker: {"listing_country": "CH"}
            for ticker in ("ALLOW", "REJECT", "ZERO", "MISSING")
        }
        metadata["US"] = {"listing_country": "US"}
        result = strategy_price_liquidity_reversion.filter_reversion_candidates(
            rows,
            {"ALLOW": 0.2, "REJECT": 0.2, "ZERO": 0.0},
            metadata,
            policy,
        )
        self.assertEqual(
            [row["ticker"] for row in result],
            ["ALLOW", "MISSING", "US"],
        )

    def test_exact_and_unit_safe_residual_contracts(self):
        frames = _brain_frames()
        as_of = next(iter(frames.values())).index[260]
        exact_inputs = {
            ticker: strategy_price_liquidity_reversion._brain_alpha_inputs(
                frame, as_of, "EXACT"
            )
            for ticker, frame in frames.items()
        }
        exact = strategy_price_liquidity_reversion._cross_sectional_residuals(
            exact_inputs,
            intercept=False,
        )
        self.assertEqual(len(exact), 20)
        self.assertAlmostEqual(
            sum(exact[ticker] * exact_inputs[ticker][1] for ticker in exact),
            0.0,
            places=10,
        )

        unit_inputs = {
            ticker: strategy_price_liquidity_reversion._brain_alpha_inputs(
                frame, as_of, "UNIT_SAFE"
            )
            for ticker, frame in frames.items()
        }
        unit_safe = strategy_price_liquidity_reversion._cross_sectional_residuals(
            unit_inputs,
            intercept=True,
        )
        predictor_mean = sum(value[1] for value in unit_inputs.values()) / 20
        self.assertAlmostEqual(sum(unit_safe.values()), 0.0, places=10)
        self.assertAlmostEqual(
            sum(
                unit_safe[ticker]
                * (unit_inputs[ticker][1] - predictor_mean)
                for ticker in unit_safe
            ),
            0.0,
            places=10,
        )

    def test_future_rows_and_future_listing_do_not_change_past_scores(self):
        frames = _brain_frames()
        metadata = {
            ticker: {"listing_country": "JP"} for ticker in frames
        }
        as_of = next(iter(frames.values())).index[260]
        policy = strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_UNIT_SAFE
        before = strategy_price_liquidity_reversion.brain_alpha_scores(
            frames,
            metadata,
            as_of,
            policy,
        )
        reordered = strategy_price_liquidity_reversion.brain_alpha_scores(
            dict(reversed(list(frames.items()))),
            metadata,
            as_of,
            policy,
        )
        self.assertEqual(before, reordered)
        extended = {}
        for ticker, frame in frames.items():
            future_dates = pd.date_range(
                frame.index[-1] + pd.offsets.BDay(1), periods=20, freq="B"
            )
            future = pd.DataFrame(
                {"Close": [9999.0 + index for index in range(20)]},
                index=future_dates,
            )
            extended[ticker] = pd.concat([frame, future])
        future_dates = pd.date_range(as_of + pd.offsets.BDay(1), periods=40, freq="B")
        extended["NEW"] = pd.DataFrame(
            {"Close": range(100, 140)},
            index=future_dates,
        )
        metadata["NEW"] = {"listing_country": "JP"}
        after = strategy_price_liquidity_reversion.brain_alpha_scores(
            extended,
            metadata,
            as_of,
            policy,
        )
        self.assertEqual(set(before), set(after))
        for ticker in before:
            self.assertAlmostEqual(before[ticker], after[ticker], places=12)

    def test_current_scan_overlay_uses_and_removes_internal_signal_date(self):
        frames = _brain_frames()
        metadata = {
            ticker: {"listing_country": "JP"} for ticker in frames
        }
        policy = strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_UNIT_SAFE
        latest = next(iter(frames.values())).index[-1]
        scores = strategy_price_liquidity_reversion.brain_alpha_scores(
            frames,
            metadata,
            latest,
            policy,
        )
        ticker, alpha = next(
            (ticker, score) for ticker, score in scores.items() if score != 0.0
        )
        overlay = strategy_price_liquidity_reversion.build_brain_alpha_trade_overlay(
            policy,
            price_loader=lambda requested, period_days=None: frames[requested],
            metadata_loader=lambda _tickers: metadata,
        )
        result = overlay(
            [{
                "ticker": ticker,
                "side": "LONG" if alpha > 0.0 else "SHORT",
                "score": 1.0 if alpha > 0.0 else -1.0,
            }],
            list(frames),
        )
        self.assertEqual(len(result), 1)
        self.assertNotIn("_brain_signal_date", result[0])
        self.assertAlmostEqual(result[0]["brain_alpha_score"], alpha)

    def test_insufficient_history_and_small_cohort_fall_back(self):
        frames = _brain_frames(count=19, periods=251)
        metadata = {
            ticker: {"listing_country": "KR"} for ticker in frames
        }
        scores = strategy_price_liquidity_reversion.brain_alpha_scores(
            frames,
            metadata,
            next(iter(frames.values())).index[-1],
            strategy_price_liquidity_reversion.BRAIN_ALPHA_APAC_EXACT,
        )
        self.assertEqual(scores, {})
        constant = {f"T{index:02d}": (1.0, 0.0) for index in range(20)}
        self.assertEqual(
            strategy_price_liquidity_reversion._cross_sectional_residuals(
                constant,
                intercept=False,
            ),
            {},
        )
        self.assertEqual(
            strategy_price_liquidity_reversion._cross_sectional_residuals(
                constant,
                intercept=True,
            ),
            {},
        )

    @patch.object(
        strategy_price_liquidity_reversion.core_universe_backtest,
        "format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch.object(
        strategy_price_liquidity_reversion.core_universe_backtest,
        "aggregate_universe_backtest",
    )
    @patch.object(
        strategy_price_liquidity_reversion,
        "analyze_value_reversion",
    )
    def test_scan_reports_current_is_and_oos_progress_safely(
        self,
        analyze,
        aggregate,
        _format,
    ):
        analyze.side_effect = [
            {"status": "NO_SIGNAL"},
            {"status": "DATA_INSUFFICIENT"},
        ]
        events = []

        def progress(stage, completed, total, ticker, succeeded):
            events.append((stage, completed, total, ticker, succeeded))
            raise RuntimeError("Telegram edit failed")

        def aggregate_result(*_args, **kwargs):
            callback = kwargs["progress_callback"]
            callback(1, 2, "AAA", True)
            callback(2, 2, "BBB", False)
            return {}

        aggregate.side_effect = aggregate_result

        report = strategy_price_liquidity_reversion.scan_value_reversion(
            ["AAA", "BBB"],
            progress_callback=progress,
        )

        self.assertIn("Reversion Scan Report", report)
        self.assertEqual(
            events,
            [
                ("CURRENT_SCAN", 1, 2, "AAA", True),
                ("CURRENT_SCAN", 2, 2, "BBB", False),
                ("IS_BACKTEST", 1, 2, "AAA", True),
                ("IS_BACKTEST", 2, 2, "BBB", False),
                ("OOS_BACKTEST", 1, 2, "AAA", True),
                ("OOS_BACKTEST", 2, 2, "BBB", False),
            ],
        )

    def test_confirmed_shock_generates_next_open_long_signal(self):
        result = strategy_price_liquidity_reversion.analyze_value_reversion(
            "TEST", df=_long_reversion_frame().iloc[:67]
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["side"], "LONG")
        self.assertNotIn("signal_date", result)

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

    def test_b_exit_scales_at_half_sigma_then_mean(self):
        frame = _long_reversion_frame()
        avg_return, win_rate, count, _, details = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST", "LONG", df=frame, include_trades=True
            )
        )
        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(avg_return, 9.31, places=2)
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(details[0]["entry_date"], "2025-04-04")
        self.assertEqual(details[0]["entry_price"], 102.0)
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertEqual(details[0]["exit_events"][1]["reason"], "mean reversion")

        cutoff = frame.index[-1]
        future = frame.iloc[[-1]].copy()
        future.index = [cutoff + pd.offsets.BDay(1)]
        future.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        extended = pd.concat([frame, future])
        cutoff_result = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST",
                "LONG",
                df=extended,
                include_trades=True,
                evaluation_end=cutoff,
            )
        )
        self.assertEqual(cutoff_result[4], details)

    def test_open_position_is_marked_at_evaluation_close(self):
        frame = _long_reversion_frame().iloc[:68]

        _, _, count, _, details = (
            strategy_price_liquidity_reversion.run_reversion_backtest(
                "TEST",
                "LONG",
                df=frame,
                include_trades=True,
            )
        )

        self.assertEqual(count, 1)
        self.assertEqual(
            details[0]["exit_events"][-1]["reason"],
            "evaluation end mark",
        )
        self.assertEqual(
            details[0]["exit_events"][-1]["date"],
            frame.index[-1].date().isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
