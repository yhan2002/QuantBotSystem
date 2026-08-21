import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import core_entry_execution
import strategy_box_range


def _channel_history(slope=0.0, periods=40):
    rows = []
    for index in range(periods):
        center = 100.0 + slope * index
        cycle = 4.0 * np.sin(2.0 * np.pi * index / 16.0)
        close = center + cycle
        rows.append(
            {
                "Open": close - 0.15,
                "High": close + 0.45,
                "Low": close - 0.45,
                "Close": close,
                "Volume": 1000.0,
            }
        )
    return pd.DataFrame(
        rows, index=pd.date_range("2025-01-01", periods=periods, freq="B")
    )


def _append_signal_and_targets(history, setup):
    stats = strategy_box_range._channel_stats(history)
    signal_levels = strategy_box_range._project_levels(stats, 1)
    lower = signal_levels["lowest"]
    width = signal_levels["highest"] - lower
    if setup == "range":
        signal_close = lower + width * 0.18
        signal_open = signal_close - 0.30
        signal_low = lower - 0.50
        signal_high = signal_close + 0.30
    else:
        signal_close = lower + min(stats["atr"] * 0.80, width * 0.25)
        signal_open = signal_close - 0.35
        signal_low = lower - stats["atr"] * 0.10
        signal_high = signal_close + 0.25
    signal = pd.DataFrame(
        [
            {
                "Open": signal_open,
                "High": signal_high,
                "Low": signal_low,
                "Close": signal_close,
                "Volume": 1000.0,
            }
        ],
        index=[history.index[-1] + pd.offsets.BDay()],
    )
    status, signal_stats = strategy_box_range._entry_signal(
        history, signal.iloc[0]
    )
    entry_levels = strategy_box_range._project_levels(signal_stats, 1)
    entry = (entry_levels["lowest"] + entry_levels["first_target"]) / 2.0
    target_day = pd.DataFrame(
        [
            {
                "Open": entry,
                "High": entry_levels["highest"] + 1.0,
                "Low": entry * 0.99,
                "Close": entry_levels["highest"],
                "Volume": 1000.0,
            },
            {
                "Open": entry_levels["highest"],
                "High": entry_levels["highest"] + 0.5,
                "Low": entry_levels["highest"] - 0.5,
                "Close": entry_levels["highest"],
                "Volume": 1000.0,
            },
        ],
        index=pd.date_range(
            signal.index[-1] + pd.offsets.BDay(), periods=2, freq="B"
        ),
    )
    return pd.concat([history, signal, target_day]), status


def _append_short_signal_and_targets(history, setup):
    stats = strategy_box_range._channel_stats(history)
    signal_levels = strategy_box_range._project_levels(stats, 1)
    upper = signal_levels["highest"]
    width = upper - signal_levels["lowest"]
    if setup == "range":
        signal_close = upper - width * 0.18
        signal_open = signal_close + 0.30
        signal_high = upper + 0.50
        signal_low = signal_close - 0.30
    else:
        signal_close = upper - min(stats["atr"] * 0.80, width * 0.25)
        signal_open = signal_close + 0.35
        signal_high = upper + stats["atr"] * 0.10
        signal_low = signal_close - 0.25
    signal = pd.DataFrame(
        [
            {
                "Open": signal_open,
                "High": signal_high,
                "Low": signal_low,
                "Close": signal_close,
                "Volume": 1000.0,
            }
        ],
        index=[history.index[-1] + pd.offsets.BDay()],
    )
    status, signal_stats = strategy_box_range._entry_signal(
        history, signal.iloc[0]
    )
    entry_levels = strategy_box_range._project_levels(signal_stats, 1)
    entry = (entry_levels["first_target"] + entry_levels["highest"]) / 2.0
    target_day = pd.DataFrame(
        [
            {
                "Open": entry,
                "High": entry * 1.01,
                "Low": entry_levels["lowest"] - 1.0,
                "Close": entry_levels["lowest"],
                "Volume": 1000.0,
            },
            {
                "Open": entry_levels["lowest"],
                "High": entry_levels["lowest"] + 0.5,
                "Low": entry_levels["lowest"] - 0.5,
                "Close": entry_levels["lowest"],
                "Volume": 1000.0,
            },
        ],
        index=pd.date_range(
            signal.index[-1] + pd.offsets.BDay(), periods=2, freq="B"
        ),
    )
    return pd.concat([history, signal, target_day]), status


def _hybrid_quiet_frame(side="LONG", periods=6):
    builder = (
        _append_signal_and_targets
        if side == "LONG"
        else _append_short_signal_and_targets
    )
    frame, _ = builder(_channel_history(0.0), "range")
    frame = frame.iloc[:41]
    _, stats = strategy_box_range._entry_signal(frame.iloc[:40], frame.iloc[40])
    levels = strategy_box_range._project_levels(stats, 1)
    entry = (
        (levels["lowest"] + levels["first_target"]) / 2.0
        if side == "LONG"
        else (levels["first_target"] + levels["highest"]) / 2.0
    )
    quiet = pd.DataFrame(
        {
            "Open": entry,
            "High": entry * 1.001,
            "Low": entry * 0.999,
            "Close": entry,
            "Volume": 1000.0,
        },
        index=pd.date_range(
            frame.index[-1] + pd.offsets.BDay(), periods=periods, freq="B"
        ),
    )
    return pd.concat([frame, quiet]), quiet, stats, entry


class BoxRangeV2Tests(unittest.TestCase):
    def test_structure_classifier_separates_flat_rising_falling(self):
        flat = strategy_box_range._channel_stats(_channel_history(0.0))
        rising = strategy_box_range._channel_stats(_channel_history(0.45))
        falling = strategy_box_range._channel_stats(_channel_history(-0.45))

        self.assertEqual(flat["structure"], "FLAT")
        self.assertEqual(rising["structure"], "RISING")
        self.assertEqual(falling["structure"], "FALLING")

    def test_flat_lower_reclaim_generates_long_signal(self):
        frame, status = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        self.assertEqual(status, "LONG_RANGE_RECLAIM")
        result = strategy_box_range.analyze_box_range(
            "TEST", df=frame.iloc[:41]
        )
        self.assertEqual(result["status"], "LONG_RANGE_RECLAIM")

    def test_rising_support_rebound_generates_long_signal(self):
        frame, status = _append_signal_and_targets(
            _channel_history(0.45), "rising"
        )
        self.assertEqual(status, "LONG_TREND_PULLBACK")
        result = strategy_box_range.analyze_box_range(
            "TEST", df=frame.iloc[:41]
        )
        self.assertEqual(result["status"], "LONG_TREND_PULLBACK")

    def test_flat_upper_reclaim_generates_short_signal(self):
        frame, status = _append_short_signal_and_targets(
            _channel_history(0.0), "range"
        )
        self.assertEqual(status, "SHORT_RANGE_RECLAIM")
        result = strategy_box_range.analyze_box_range(
            "TEST", df=frame.iloc[:41]
        )
        self.assertEqual(result["status"], "SHORT_RANGE_RECLAIM")

    def test_falling_resistance_rejection_generates_short_signal(self):
        history = _channel_history(-0.45)
        frame, status = _append_short_signal_and_targets(
            history, "falling"
        )
        self.assertEqual(status, "SHORT_TREND_RALLY")
        result = strategy_box_range.analyze_box_range(
            "TEST", df=frame.iloc[:41]
        )
        self.assertEqual(result["status"], "SHORT_TREND_RALLY")

    def test_precompute_does_not_use_future_bars(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        altered = frame.copy()
        altered.iloc[42, altered.columns.get_loc("Close")] *= 3.0
        altered.iloc[42, altered.columns.get_loc("High")] *= 3.0

        original = strategy_box_range._precompute_entry_candidates(frame)
        changed = strategy_box_range._precompute_entry_candidates(altered)

        self.assertEqual(original["status"][40], changed["status"][40])
        self.assertAlmostEqual(
            original["stats"][40]["lowest"],
            changed["stats"][40]["lowest"],
        )

    def test_next_open_gate_requires_support_to_first_target(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        _, stats = strategy_box_range._entry_signal(
            frame.iloc[:40], frame.iloc[40]
        )
        levels = strategy_box_range._project_levels(stats, 1)
        self.assertFalse(
            strategy_box_range._valid_next_open(
                levels["lowest"], stats, "LONG"
            )
        )
        self.assertFalse(
            strategy_box_range._valid_next_open(
                levels["first_target"], stats, "LONG"
            )
        )
        self.assertTrue(
            strategy_box_range._valid_next_open(
                (levels["lowest"] + levels["first_target"]) / 2.0,
                stats,
                "LONG",
            )
        )

    def test_backtest_outputs_setup_and_split_exit_dates(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        _, win_rate, count, _, details = (
            strategy_box_range.run_box_backtest(
                "TEST", df=frame, include_trades=True
            )
        )
        self.assertEqual(count, 1)
        self.assertEqual(win_rate, 100.0)
        self.assertEqual(details[0]["side"], "LONG")
        self.assertEqual(details[0]["setup"], "RANGE_RECLAIM")
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(len(details[0]["exit_events"]), 2)
        self.assertTrue(
            all(event["date"] for event in details[0]["exit_events"])
        )

        cutoff = frame.index[-1]
        future = frame.iloc[[-1]].copy()
        future.index = [cutoff + pd.offsets.BDay(1)]
        future.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        extended = pd.concat([frame, future])
        cutoff_result = strategy_box_range.run_box_backtest(
            "TEST",
            df=extended,
            include_trades=True,
            evaluation_end=cutoff,
        )
        self.assertEqual(cutoff_result[4], details)

    def test_fixed_exit_ignores_native_targets_and_closes_at_d_plus_five(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        extension = pd.DataFrame(
            [frame.iloc[-1].to_dict()] * 10,
            index=pd.date_range(
                frame.index[-1] + pd.offsets.BDay(1), periods=10, freq="B"
            ),
        )
        frame = pd.concat([frame, extension])

        result = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame,
            include_trades=True,
            fixed_exit_sessions=5,
        )

        trade = result[4][0]
        entry_index = frame.index.get_loc(pd.Timestamp(trade["entry_date"]))
        incomplete = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame.iloc[: entry_index + 5],
            include_trades=True,
            fixed_exit_sessions=5,
        )
        self.assertEqual(1, result[2])
        self.assertEqual(1, len(trade["exit_events"]))
        self.assertEqual("D+5 fixed close", trade["exit_events"][0]["reason"])
        self.assertEqual(
            5,
            frame.index.get_loc(pd.Timestamp(trade["exit_events"][0]["date"]))
            - frame.index.get_loc(pd.Timestamp(trade["entry_date"])),
        )
        self.assertEqual(0, incomplete[2])

    def test_hybrid_exit_closes_long_and_short_exactly_at_d_plus_five(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                frame, quiet, _, _ = _hybrid_quiet_frame(side)
                result = strategy_box_range.run_box_backtest(
                    "TEST",
                    df=frame,
                    side_filter=side,
                    include_trades=True,
                    hybrid_exit_sessions=5,
                )
                incomplete = strategy_box_range.run_box_backtest(
                    "TEST",
                    df=frame.iloc[:-1],
                    side_filter=side,
                    include_trades=True,
                    hybrid_exit_sessions=5,
                )

                self.assertEqual(1, result[2])
                self.assertEqual(0, incomplete[2])
                self.assertEqual(
                    "D+5 hybrid close",
                    result[4][0]["exit_events"][-1]["reason"],
                )
                self.assertEqual(
                    quiet.index[-1].date().isoformat(),
                    result[4][0]["exit_events"][-1]["date"],
                )

    def test_hybrid_exit_defers_after_a_d5_partial_target(self):
        frame, quiet, stats, entry = _hybrid_quiet_frame("LONG", periods=7)
        d5_levels = strategy_box_range._project_levels(stats, 6)
        quiet.iloc[5, quiet.columns.get_loc("High")] = (
            d5_levels["first_target"] + 0.01
        )
        quiet.iloc[5, quiet.columns.get_loc("Close")] = entry
        frame.iloc[-7:] = quiet

        result = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame,
            include_trades=True,
            hybrid_exit_sessions=5,
        )

        events = result[4][0]["exit_events"]
        self.assertEqual(
            ["first target 50%", "D+5 hybrid close"],
            [event["reason"] for event in events],
        )
        self.assertEqual(quiet.index[5].date().isoformat(), events[0]["date"])
        self.assertEqual(quiet.index[6].date().isoformat(), events[1]["date"])

    def test_hybrid_exit_keeps_native_targets_and_rejects_fixed_combination(self):
        frame, _ = _append_signal_and_targets(_channel_history(0.0), "range")
        result = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame,
            include_trades=True,
            hybrid_exit_sessions=5,
        )

        self.assertEqual(
            ["first target 50%", "channel upper target"],
            [event["reason"] for event in result[4][0]["exit_events"]],
        )
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            strategy_box_range.run_box_backtest(
                "TEST",
                df=frame,
                fixed_exit_sessions=5,
                hybrid_exit_sessions=5,
            )

    def test_hybrid_exit_keeps_native_stop_first_for_long_and_short(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                frame, quiet, _, entry = _hybrid_quiet_frame(side)
                if side == "LONG":
                    quiet.iloc[0, quiet.columns.get_loc("Low")] = entry * 0.89
                    quiet.iloc[0, quiet.columns.get_loc("High")] = entry * 1.50
                else:
                    quiet.iloc[0, quiet.columns.get_loc("High")] = entry * 1.11
                    quiet.iloc[0, quiet.columns.get_loc("Low")] = entry * 0.50
                frame.iloc[-len(quiet) :] = quiet

                result = strategy_box_range.run_box_backtest(
                    "TEST",
                    df=frame,
                    side_filter=side,
                    include_trades=True,
                    hybrid_exit_sessions=5,
                )

                self.assertEqual(
                    ["-7% stop", "-10% stop"],
                    [event["reason"] for event in result[4][0]["exit_events"]],
                )

    def test_open_position_is_marked_at_evaluation_close(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        frame = frame.iloc[:42]
        entry_open = float(frame["Open"].iloc[-1])
        frame.iloc[-1, frame.columns.get_loc("High")] = entry_open * 1.001
        frame.iloc[-1, frame.columns.get_loc("Low")] = entry_open * 0.999
        frame.iloc[-1, frame.columns.get_loc("Close")] = entry_open

        _, _, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=frame, include_trades=True
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

    def test_short_backtest_outputs_short_setup_and_split_exits(self):
        frame, _ = _append_short_signal_and_targets(
            _channel_history(0.0), "range"
        )
        _, win_rate, count, _, details = strategy_box_range.run_box_backtest(
            "TEST", df=frame, side_filter="SHORT", include_trades=True
        )
        self.assertEqual(count, 1)
        self.assertEqual(win_rate, 100.0)
        self.assertEqual(details[0]["side"], "SHORT")
        self.assertEqual(details[0]["setup"], "RANGE_RECLAIM")
        self.assertLess(
            details[0]["signal_date"], details[0]["entry_date"]
        )
        self.assertEqual(len(details[0]["exit_events"]), 2)

    def test_side_filters_exclude_opposite_direction(self):
        long_frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        short_frame, _ = _append_short_signal_and_targets(
            _channel_history(0.0), "range"
        )
        long_result = strategy_box_range.run_box_backtest(
            "TEST", df=long_frame, side_filter="SHORT", include_trades=True
        )
        short_result = strategy_box_range.run_box_backtest(
            "TEST", df=short_frame, side_filter="LONG", include_trades=True
        )
        self.assertEqual(long_result[2], 0)
        self.assertEqual(short_result[2], 0)

    def test_backtest_reuses_index_aligned_precomputed_candidates(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        candidates = strategy_box_range._precompute_entry_candidates(frame)
        expected = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame,
            include_trades=True,
        )
        with patch(
            "strategy_box_range._precompute_entry_candidates",
            side_effect=AssertionError("candidate cache was not reused"),
        ):
            actual = strategy_box_range.run_box_backtest(
                "TEST",
                df=frame,
                include_trades=True,
                precomputed_candidates=candidates,
            )

        self.assertEqual(actual, expected)

    @patch(
        "strategy_box_range.core_universe_backtest.format_fixed_is_oos_backtest",
        return_value="\nSUMMARY",
    )
    @patch(
        "strategy_box_range.core_universe_backtest.aggregate_universe_backtest",
        return_value={},
    )
    @patch("strategy_box_range._load_ohlcv")
    def test_scan_respects_short_only_and_loads_each_ticker_once(
        self, load_ohlcv, aggregate, _format
    ):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        load_ohlcv.return_value = frame
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

        report = strategy_box_range.scan_total_universe(
            ["AAA", "BBB"],
            position_mode="SHORT_ONLY",
            progress_callback=progress,
        )

        self.assertEqual(load_ohlcv.call_count, 2)
        self.assertIn("Position mode: Short Only", report)
        self.assertIn("SHORT Candidates (next open)", report)
        self.assertEqual(aggregate.call_count, 2)
        self.assertEqual(
            [event[0] for event in events],
            [
                "CURRENT_SCAN",
                "CURRENT_SCAN",
                "IS_BACKTEST",
                "IS_BACKTEST",
                "OOS_BACKTEST",
                "OOS_BACKTEST",
            ],
        )
        for call in aggregate.call_args_list:
            self.assertEqual(call.kwargs["side_filter"], "SHORT")

        selection_runner = aggregate.call_args_list[0].args[1]
        oos_runner = aggregate.call_args_list[1].args[1]
        with patch(
            "strategy_box_range._precompute_entry_candidates",
            wraps=strategy_box_range._precompute_entry_candidates,
        ) as precompute:
            selection_runner("AAA", include_trades=True)
            oos_runner("AAA", include_trades=True)
        self.assertEqual(precompute.call_count, 1)

    @patch.object(strategy_box_range, "scan_total_universe", return_value="report")
    def test_public_scan_forwards_progress_callback(self, scan_total):
        callback = object()

        result = strategy_box_range.scan_box_range(
            ["AAA"],
            progress_callback=callback,
        )

        self.assertEqual(result, "report")
        self.assertIs(scan_total.call_args.kwargs["progress_callback"], callback)

    def test_ohlc4_entry_does_not_exit_during_execution_day(self):
        frame, _ = _append_signal_and_targets(
            _channel_history(0.0), "range"
        )
        _, _, count, _, details = strategy_box_range.run_box_backtest(
            "TEST",
            df=frame,
            include_trades=True,
            entry_execution=core_entry_execution.NEXT_DAY_OHLC4,
        )
        self.assertEqual(count, 1)
        self.assertNotEqual(
            details[0]["exit_events"][0]["date"],
            details[0]["entry_date"],
        )


if __name__ == "__main__":
    unittest.main()
