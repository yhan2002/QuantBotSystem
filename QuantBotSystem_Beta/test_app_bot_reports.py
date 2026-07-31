import unittest
from unittest.mock import patch

import app_bot_reports
import config
import core_universe_backtest


class BotReportTests(unittest.TestCase):
    def test_nasdaq_top_30_snapshot_membership(self):
        expected = {
            "AAPL", "NVDA", "SNDK", "GOOGL", "MSFT", "AMZN", "AVGO", "SPCX", "META", "TSLA",
            "MU", "WMT", "SKHY", "AMD", "ASML", "INTC", "CSCO", "COST", "AMAT", "LRCX",
            "NFLX", "PLTR", "PANW", "ARM", "TXN", "KLAC", "LIN", "AMGN", "STX", "PEP",
        }
        self.assertEqual(expected, set(config.universe_groups["NASDAQ_TOP_30_20260731"]))

    def test_quick_report_uses_v2_box_status_and_side(self):
        box = {
            "status": "LONG_TREND_PULLBACK",
            "box_width_pct": 0.12,
            "message": "synthetic channel",
        }
        with (
            patch("app_bot_reports.core_market_data_store.ensure_price_data"),
            patch(
                "app_bot_reports.strategy_ma_cross.analyze_trend_strict_signals",
                return_value=("NO_SIGNAL", 0.0),
            ),
            patch(
                "app_bot_reports.strategy_box_range.analyze_box_range",
                return_value=box,
            ),
            patch(
                "app_bot_reports.strategy_price_liquidity_reversion.analyze_value_reversion",
                return_value={"status": "NO_SIGNAL"},
            ),
            patch(
                "app_bot_reports.strategy_ma_cross.run_isolated_backtest",
                return_value=(0.0, 0.0, 0, "6M"),
            ),
            patch(
                "app_bot_reports.strategy_box_range.run_box_backtest",
                return_value=(0.0, 0.0, 0, "6M"),
            ) as box_backtest,
        ):
            report = app_bot_reports.quick_report("SYNTH")

        box_backtest.assert_called_once_with(
            "SYNTH", side_filter="LONG", period="6mo"
        )
        self.assertIn("LONG_TREND_PULLBACK", report)
        self.assertIn("prior 40 completed bars", report)

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
        scan.assert_called_once_with(
            ["AAA"],
            neutralization="NONE",
            position_mode="LONG_SHORT_NEUTRAL",
        )


if __name__ == "__main__":
    unittest.main()
