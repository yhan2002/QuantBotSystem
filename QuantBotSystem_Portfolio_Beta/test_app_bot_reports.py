import unittest
from unittest.mock import patch

import app_bot_reports
import core_universe_backtest


class BotReportTests(unittest.TestCase):
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
        scan.assert_called_once_with(
            ["AAA"],
            neutralization="NONE",
            position_mode="LONG_SHORT_NEUTRAL",
        )

    @patch("app_bot_reports.core_market_data_store.freshness_summary", return_value="fresh")
    @patch("app_bot_reports.core_market_data_store.get_missing_tickers", return_value=[])
    @patch("app_bot_reports.strategy_residual_stat_arb_app.scan_residual_stat_arb")
    @patch("app_bot_reports.compile_selected_universe", return_value=["AAA", "BBB"])
    @patch("app_bot_reports.get_or_create_session")
    def test_residual_strategy_dispatches_through_full_universe_adapter(
        self, get_session, _compile, scan, _missing, _freshness
    ):
        session = {
            "strategy": "RESIDUAL_STAT_ARB",
            "custom_tickers": [],
            "neutralization": "SECTOR",
            "position_mode": "LONG_ONLY",
        }
        get_session.return_value = session
        scan.return_value = "residual report"

        report = app_bot_reports.run_compiled_scan("any-chat")

        self.assertIn("residual report", report)
        scan.assert_called_once_with(
            ["AAA", "BBB"],
            neutralization="SECTOR",
            position_mode="LONG_ONLY",
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


if __name__ == "__main__":
    unittest.main()
