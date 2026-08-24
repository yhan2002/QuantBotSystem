import unittest
from unittest.mock import patch

import app_bot_reports
import app_bot_session
import core_universe_backtest


class BotReportTests(unittest.TestCase):
    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_box_range.scan_box_range", return_value="summary")
    @patch("app_bot_reports.get_or_create_session", return_value={})
    def test_box_hybrid_policy_is_forwarded_only_to_box_scan(
        self, _session, scan, _missing, _freshness
    ):
        request = app_bot_reports.ScanRequest(
            universe=("AAA",),
            strategy="BOX_RANGE",
            exit_policy=app_bot_session.BOX_HYBRID_EXIT_POLICY,
            neutralization="NONE",
            position_mode="LONG_SHORT_NEUTRAL",
            custom_tickers=frozenset(),
        )

        app_bot_reports.run_compiled_scan("any-chat", request=request)

        self.assertIsNone(scan.call_args.kwargs["fixed_exit_sessions"])
        self.assertEqual(
            5,
            scan.call_args.kwargs["hybrid_exit_sessions"],
        )

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_box_range_new.scan_box_range_new", return_value="summary")
    @patch("app_bot_reports.get_or_create_session", return_value={})
    def test_new_box_scan_reuses_common_contract_without_legacy_hybrid(
        self, _session, scan, _missing, _freshness
    ):
        request = app_bot_reports.ScanRequest(
            universe=("AAA",),
            strategy="BOX_RANGE_NEW",
            exit_policy=app_bot_session.BOX_HYBRID_EXIT_POLICY,
            neutralization="SECTOR",
            position_mode="LONG_ONLY",
            custom_tickers=frozenset(),
        )

        app_bot_reports.run_compiled_scan("any-chat", request=request)

        self.assertNotIn("hybrid_exit_sessions", scan.call_args.kwargs)
        self.assertIsNone(scan.call_args.kwargs["fixed_exit_sessions"])
        self.assertEqual("SECTOR", scan.call_args.kwargs["neutralization"])
        self.assertEqual("LONG_ONLY", scan.call_args.kwargs["position_mode"])
        self.assertEqual({}, scan.call_args.kwargs["report_artifacts"])

    def test_quick_report_includes_new_box_signal_and_backtest(self):
        with patch.object(
            app_bot_reports.core_market_data_store,
            "ensure_price_data",
        ), patch.object(
            app_bot_reports.strategy_ma_cross,
            "analyze_trend_strict_signals",
            return_value=("NO_SIGNAL", 0.0),
        ), patch.object(
            app_bot_reports.strategy_box_range,
            "analyze_box_range",
            return_value={"status": "NORMAL", "box_width_pct": 0.1},
        ), patch.object(
            app_bot_reports.strategy_box_range_new,
            "analyze_box_range_new",
            return_value={
                "status": "LONG_BREAKOUT",
                "box_width_pct": 0.03,
                "score": 0.7,
            },
        ), patch.object(
            app_bot_reports.strategy_price_liquidity_reversion,
            "analyze_value_reversion",
            return_value={"status": "NO_SIGNAL"},
        ), patch.object(
            app_bot_reports.strategy_ma_cross,
            "run_isolated_backtest",
            return_value=(0.0, 0.0, 0, "6M"),
        ), patch.object(
            app_bot_reports.strategy_box_range,
            "run_box_backtest",
            return_value=(0.0, 0.0, 0, "6M"),
        ), patch.object(
            app_bot_reports.strategy_box_range_new,
            "run_box_backtest",
            return_value=(1.2, 50.0, 4, "6M"),
        ):
            report = app_bot_reports.quick_report("AAA")

        self.assertIn("Box Range (New one): `LONG_BREAKOUT`", report)
        self.assertIn("prior 15 completed bars", report)
        self.assertIn("Box Range (New one) | LONG: avg trade +1.2%", report)

    @patch("app_bot_reports.compile_selected_universe", return_value=["AAA"])
    @patch("app_bot_reports.get_or_create_session")
    def test_scan_request_is_an_immutable_settings_snapshot(
        self,
        get_session,
        _compile,
    ):
        session = {
            "strategy": "BOX_RANGE",
            "exit_policy": "FIXED_5D",
            "custom_tickers": ["AAA"],
            "neutralization": "COUNTRY",
            "position_mode": "LONG_ONLY",
        }
        get_session.return_value = session

        request = app_bot_reports.snapshot_scan_request("any-chat")
        session["strategy"] = "MA_CROSS"
        session["custom_tickers"].append("BBB")

        self.assertEqual("BOX_RANGE", request.strategy)
        self.assertEqual("FIXED_5D", request.exit_policy)
        self.assertEqual(("AAA",), request.universe)
        self.assertEqual(frozenset({"AAA"}), request.custom_tickers)
        self.assertEqual("COUNTRY", request.neutralization)
        self.assertEqual("LONG_ONLY", request.position_mode)

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_ma_cross.scan_ma_cross")
    @patch("app_bot_reports.compile_selected_universe", return_value=["AAA"])
    @patch("app_bot_reports.get_or_create_session")
    def test_scan_always_keeps_detail_for_see_detail_button(
        self, get_session, _compile, scan, _missing, _freshness
    ):
        session = {"strategy": "MA_CROSS", "custom_tickers": []}
        get_session.return_value = session
        scan.return_value = (
            "summary" + core_universe_backtest.DETAIL_MARKER + "developer detail"
        )

        report = app_bot_reports.run_compiled_scan("any-chat")

        self.assertIn("summary", report)
        self.assertNotIn("developer detail", report)
        self.assertEqual(session["last_backtest_detail"], "developer detail")
        self.assertIsNone(session["last_backtest_csv_path"])
        self.assertIn(
            session["last_backtest_report_id"],
            session["backtest_reports"],
        )
        scan.assert_called_once()
        self.assertEqual(["AAA"], scan.call_args.args[0])
        self.assertEqual("NONE", scan.call_args.kwargs["neutralization"])
        self.assertEqual(
            "LONG_SHORT_NEUTRAL",
            scan.call_args.kwargs["position_mode"],
        )
        self.assertEqual({}, scan.call_args.kwargs["report_artifacts"])
        self.assertIsNone(scan.call_args.kwargs["fixed_exit_sessions"])

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_ma_cross.scan_ma_cross")
    @patch("app_bot_reports.compile_selected_universe", return_value=["AAA"])
    @patch("app_bot_reports.get_or_create_session")
    def test_scan_stores_lazy_export_spec_with_its_report(
        self, get_session, _compile, scan, _missing, _freshness
    ):
        session = {"strategy": "MA_CROSS", "custom_tickers": []}
        get_session.return_value = session
        export_spec = {"version": 1, "strategy_key": "ma_momentum"}

        def fake_scan(_universe, **kwargs):
            kwargs["report_artifacts"]["backtest_export"] = export_spec
            return (
                "summary"
                + core_universe_backtest.DETAIL_MARKER
                + "developer detail"
            )

        scan.side_effect = fake_scan

        _, report_id = app_bot_reports.run_compiled_scan(
            "any-chat",
            return_report_id=True,
        )

        stored = app_bot_session.get_backtest_report(session, report_id)
        self.assertEqual(
            export_spec,
            stored["export_spec"],
        )
        self.assertIsNone(
            stored["export_path"]
        )

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_ma_cross.scan_ma_cross")
    @patch("app_bot_reports.compile_selected_universe", return_value=["AAA"])
    @patch("app_bot_reports.get_or_create_session")
    def test_each_scan_keeps_an_independent_detail_payload(
        self, get_session, _compile, scan, _missing, _freshness
    ):
        session = {"strategy": "MA_CROSS", "custom_tickers": []}
        get_session.return_value = session
        scan.side_effect = [
            "one" + core_universe_backtest.DETAIL_MARKER + "detail one",
            "two" + core_universe_backtest.DETAIL_MARKER + "detail two",
        ]

        _, first_id = app_bot_reports.run_compiled_scan(
            "any-chat", return_report_id=True
        )
        _, second_id = app_bot_reports.run_compiled_scan(
            "any-chat", return_report_id=True
        )

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            session["backtest_reports"][first_id]["detail"],
            "detail one",
        )
        self.assertEqual(
            session["backtest_reports"][second_id]["detail"],
            "detail two",
        )

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_ma_cross.scan_ma_cross")
    @patch("app_bot_reports.get_or_create_session")
    def test_background_scan_uses_snapshot_and_forwards_progress(
        self,
        get_session,
        scan,
        _missing,
        _freshness,
    ):
        session = {
            "strategy": "BOX_RANGE",
            "custom_tickers": [],
        }
        get_session.return_value = session
        request = app_bot_reports.ScanRequest(
            universe=("AAA", "BBB"),
            strategy="MA_CROSS",
            exit_policy="FIXED_5D",
            neutralization="MARKET",
            position_mode="SHORT_ONLY",
            custom_tickers=frozenset(),
        )
        progress_events = []

        def fake_scan(universe, **kwargs):
            kwargs["progress_callback"](
                "CURRENT_SCAN",
                1,
                len(universe),
                "AAA",
                True,
            )
            return "summary"

        scan.side_effect = fake_scan
        report = app_bot_reports.run_compiled_scan(
            "any-chat",
            request=request,
            progress_callback=lambda *args: progress_events.append(args),
        )

        self.assertIn("summary", report)
        self.assertEqual(
            [event[0] for event in progress_events],
            ["PREPARING", "CURRENT_SCAN", "FINALIZING"],
        )
        scan.assert_called_once()
        self.assertEqual(["AAA", "BBB"], scan.call_args.args[0])
        self.assertEqual("MARKET", scan.call_args.kwargs["neutralization"])
        self.assertEqual("SHORT_ONLY", scan.call_args.kwargs["position_mode"])
        self.assertEqual(5, scan.call_args.kwargs["fixed_exit_sessions"])


if __name__ == "__main__":
    unittest.main()
