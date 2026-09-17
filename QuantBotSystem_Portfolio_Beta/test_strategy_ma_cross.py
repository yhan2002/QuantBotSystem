import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

import core_entry_execution
import strategy_ma_cross


class MomentumEvaluationMarkTests(unittest.TestCase):
    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "aggregate_universe_backtest",
    )
    @patch.object(strategy_ma_cross, "analyze_trend_strict_signals")
    def test_scan_reports_current_is_and_oos_progress_safely(
        self,
        analyze,
        aggregate,
        _format,
    ):
        analyze.side_effect = [
            ("EXIT_SIGNAL", 1.0),
            ("ERROR", 0.0),
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

        report = strategy_ma_cross.scan_ma_cross(
            ["AAA", "BBB"],
            progress_callback=progress,
        )

        self.assertIn("Momentum Scan Report", report)
        self.assertNotIn("EMA 20/60/120/200 배열 변화", report)
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

    def test_incomplete_holding_period_is_marked_at_evaluation_close(self):
        dates = pd.date_range("2024-01-01", periods=275, freq="B")
        frame = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1000.0,
            },
            index=dates,
        )
        frame.iloc[-1, frame.columns.get_loc("Close")] = 105.0
        future = frame.iloc[[-1]].copy()
        future.index = [dates[-1] + pd.offsets.BDay(1)]
        future.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        extended = pd.concat([frame, future])

        def add_signals(prices, **_kwargs):
            result = prices.copy()
            result["LONG_SIGNAL"] = False
            result["SHORT_SIGNAL"] = False
            result["SIGNAL_STRENGTH"] = 0.0
            result.iloc[-2, result.columns.get_loc("LONG_SIGNAL")] = True
            result.iloc[-2, result.columns.get_loc("SIGNAL_STRENGTH")] = 2.0
            return result

        def next_open(prices, signal_index, side, **_kwargs):
            self.assertEqual(side, "LONG")
            entry_index = signal_index + 1
            return {
                "price": float(prices["Open"].iloc[entry_index]),
                "mode": core_entry_execution.NEXT_OPEN,
                "signal_close": float(prices["Close"].iloc[signal_index]),
                "next_open": float(prices["Open"].iloc[entry_index]),
                "signal_to_open_return_pct": 0.0,
                "signal_to_fill_return_pct": 0.0,
            }

        with patch.object(
            strategy_ma_cross,
            "_add_ma_signal_columns",
            side_effect=add_signals,
        ), patch.object(
            strategy_ma_cross.core_entry_execution,
            "resolve_entry",
            side_effect=next_open,
        ):
            average, win_rate, count, _, details = (
                strategy_ma_cross.run_isolated_backtest(
                    "TEST",
                    df=frame,
                    include_trades=True,
                )
            )
            cutoff_result = strategy_ma_cross.run_isolated_backtest(
                "TEST",
                df=extended,
                include_trades=True,
                evaluation_end=dates[-1],
            )

        self.assertEqual((count, win_rate), (1, 100.0))
        self.assertAlmostEqual(average, 5.0)
        self.assertEqual(
            details[0]["signal_date"], dates[-2].date().isoformat()
        )
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(details[0]["entry_date"], dates[-1].date().isoformat())
        self.assertEqual(
            details[0]["exit_events"][0]["reason"],
            "evaluation end mark",
        )
        self.assertEqual(
            details[0]["exit_date"],
            dates[-1].date().isoformat(),
        )
        self.assertEqual(cutoff_result[4], details)

    def test_detects_three_latest_ema_alignment_transitions(self):
        dates = pd.date_range("2024-01-01", periods=400, freq="B")

        def frame(closes):
            closes = np.asarray(closes, dtype=float)
            return pd.DataFrame(
                {
                    "Open": closes,
                    "High": closes,
                    "Low": closes,
                    "Close": closes,
                    "Volume": 1000.0,
                },
                index=dates[: len(closes)],
            )

        cases = [
            (
                np.r_[np.full(220, 100.0), np.linspace(100, 160, 2)],
                "BULLISH_ALIGNMENT",
            ),
            (
                np.r_[np.full(220, 100.0), np.linspace(100, 40, 2)],
                "BEARISH_ALIGNMENT",
            ),
            (
                np.r_[
                    np.linspace(50, 150, 300),
                    np.linspace(150, 80, 30)[:12],
                ],
                "ALIGNMENT_BREAK",
            ),
        ]

        for closes, expected in cases:
            prices = frame(closes)
            with self.subTest(expected=expected), patch.object(
                strategy_ma_cross,
                "_load_ohlcv",
                return_value=prices,
            ):
                event, signal_date = strategy_ma_cross.detect_ema_alignment_event(
                    "TEST"
                )
                self.assertEqual(expected, event)
                self.assertEqual(prices.index[-1].date().isoformat(), signal_date)

    def test_completed_bar_filter_drops_intraday_row_but_keeps_past_row(self):
        closes = np.r_[np.full(220, 100.0), 100.0, 160.0, 1.0]
        dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
        prices = pd.DataFrame({"Close": closes}, index=dates)
        current_session = prices.copy()
        current_session.index = current_session.index[:-1].append(
            pd.DatetimeIndex([pd.Timestamp("2026-09-17")])
        )
        current_session.attrs["latest_updated_at"] = "2026-09-17T15:00:00+00:00"
        before_us_close = datetime(2026, 9, 17, 19, 0, tzinfo=timezone.utc)

        filtered = strategy_ma_cross._completed_daily_bars(
            current_session, "TEST", now_utc=before_us_close
        )
        past = strategy_ma_cross._completed_daily_bars(
            prices,
            "TEST",
            now_utc=datetime(2026, 9, 17, 20, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(filtered.index[-1], current_session.index[-2])
        self.assertEqual(past.index[-1], prices.index[-1])

    def test_ema_values_match_recursive_trading_formula(self):
        close = pd.Series(np.linspace(50.0, 150.0, 240))

        def recursive_ema(values, span):
            alpha = 2.0 / (span + 1.0)
            result = float(values.iloc[0])
            for value in values.iloc[1:]:
                result = alpha * float(value) + (1.0 - alpha) * result
            return result

        for window, actual in zip(
            strategy_ma_cross.EMA_ALIGNMENT_WINDOWS,
            strategy_ma_cross._ema_series(close),
        ):
            self.assertAlmostEqual(
                recursive_ema(close, window),
                float(actual.iloc[-1]),
                places=12,
            )

    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch.object(
        strategy_ma_cross.core_universe_backtest,
        "aggregate_universe_backtest",
        return_value={},
    )
    @patch.object(
        strategy_ma_cross,
        "analyze_trend_strict_signals",
        return_value=("EXIT_SIGNAL", 0.0),
    )
    @patch.object(strategy_ma_cross, "detect_ema_alignment_event")
    def test_manual_scan_reports_ema_events_with_requested_wording(
        self,
        detect,
        _analyze,
        _aggregate,
        _format,
    ):
        detect.side_effect = [
            ("BULLISH_ALIGNMENT", "2026-09-16"),
            ("BEARISH_ALIGNMENT", "2026-09-16"),
            ("ALIGNMENT_BREAK", "2026-09-16"),
        ]

        report = strategy_ma_cross.scan_ma_cross(
            ["AAA", "BBB", "CCC"],
            include_ema_alignment_events=True,
        )

        self.assertIn("정배열 신규 완성: AAA (2026-09-16)", report)
        self.assertIn("역배열 신규 완성: BBB (2026-09-16)", report)
        self.assertIn("배열 파괴: CCC (2026-09-16)", report)
        self.assertNotIn("역정배열", report)

    def test_fixed_exit_closes_at_d_plus_five_and_drops_incomplete_entries(self):
        dates = pd.date_range("2024-01-01", periods=280, freq="B")
        frame = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 111.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1000.0,
            },
            index=dates,
        )
        frame.iloc[-1, frame.columns.get_loc("Close")] = 110.0
        signal_date = dates[-7]

        def add_signals(prices, **_kwargs):
            result = prices.copy()
            result["LONG_SIGNAL"] = False
            result["SHORT_SIGNAL"] = False
            result["SIGNAL_STRENGTH"] = 0.0
            if signal_date in result.index:
                result.loc[signal_date, "LONG_SIGNAL"] = True
                result.loc[signal_date, "SIGNAL_STRENGTH"] = 2.0
            return result

        with patch.object(
            strategy_ma_cross,
            "_add_ma_signal_columns",
            side_effect=add_signals,
        ):
            result = strategy_ma_cross.run_isolated_backtest(
                "TEST",
                df=frame,
                include_trades=True,
                fixed_exit_sessions=5,
            )
            incomplete = strategy_ma_cross.run_isolated_backtest(
                "TEST",
                df=frame.iloc[:-1],
                include_trades=True,
                fixed_exit_sessions=5,
            )

        trade = result[4][0]
        self.assertEqual(1, result[2])
        self.assertEqual("D+5 fixed close", trade["exit_reason"])
        self.assertEqual(
            5,
            frame.index.get_loc(pd.Timestamp(trade["exit_date"]))
            - frame.index.get_loc(pd.Timestamp(trade["entry_date"])),
        )
        self.assertEqual(0, incomplete[2])


if __name__ == "__main__":
    unittest.main()
