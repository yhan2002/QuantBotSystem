import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_bot_session


class BotSessionTests(unittest.TestCase):
    def tearDown(self):
        app_bot_session.USER_SESSIONS.clear()

    def test_nasdaq_top_30_matches_deployment_snapshot(self):
        group_code = "NASDAQ_TOP_30_20260731"
        expected = [
            "NVDA", "GOOGL", "AAPL", "MSFT", "AMZN", "AVGO", "META", "TSLA", "WMT", "MU",
            "AMD", "ASML", "INTC", "COST", "AMAT", "LRCX", "CSCO", "NFLX", "PLTR", "AZN",
            "ARM", "LIN", "TXN", "AMGN", "PEP", "WDC", "STX", "TMUS", "ADI", "PANW",
        ]

        self.assertIn((group_code, "Nasdaq Top 30"), app_bot_session.UNIVERSE_MENUS)
        self.assertEqual(30, len(app_bot_session.config.universe_groups[group_code]))
        self.assertEqual(set(expected), set(app_bot_session.config.universe_groups[group_code]))
        self.assertEqual(
            set(expected),
            set(
                app_bot_session.compile_selected_universe(
                    {"selected_groups": {group_code}}
                )
            ),
        )

    def test_removed_strategy_falls_back_to_momentum(self):
        app_bot_session.USER_SESSIONS["legacy-chat"] = {
            "strategy": "REMOVED_STRATEGY",
        }

        session = app_bot_session.get_or_create_session("legacy-chat")

        self.assertEqual(session["strategy"], "MA_CROSS")

    def test_exit_policy_defaults_and_legacy_values_fall_back_to_native(self):
        self.assertEqual(
            app_bot_session.get_or_create_session("new-chat")["exit_policy"],
            "NATIVE",
        )
        app_bot_session.USER_SESSIONS["legacy-chat"] = {
            "strategy": "MA_CROSS",
            "exit_policy": "REMOVED_POLICY",
        }

        self.assertEqual(
            app_bot_session.get_or_create_session("legacy-chat")["exit_policy"],
            "NATIVE",
        )

        for index, policy in enumerate(app_bot_session.BOX_ONLY_EXIT_POLICIES):
            chat_id = f"wrong-strategy-{index}"
            app_bot_session.USER_SESSIONS[chat_id] = {
                "strategy": "MA_CROSS",
                "exit_policy": policy,
            }
            self.assertEqual(
                "NATIVE",
                app_bot_session.get_or_create_session(chat_id)["exit_policy"],
            )

    def test_report_ids_keep_results_independent(self):
        session = {}
        first = app_bot_session.store_backtest_report(
            session, "first detail", "first.zip"
        )
        second = app_bot_session.store_backtest_report(
            session, "second detail", "second.zip"
        )

        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertEqual(len(second), 12)
        self.assertEqual(
            app_bot_session.get_backtest_report(session, first)["detail"],
            "first detail",
        )
        self.assertEqual(
            app_bot_session.get_backtest_report(session, second)["detail"],
            "second detail",
        )
        self.assertIsNone(
            app_bot_session.get_backtest_report(session, "expired")
        )

    def test_report_keeps_positional_export_path_and_lazy_export_spec(self):
        session = {}
        export_spec = {"version": 1, "selection_result": {"AAA": "BUY"}}

        report_id = app_bot_session.store_backtest_report(
            session,
            "detail",
            "legacy.zip",
            export_spec=export_spec,
        )

        self.assertEqual(
            app_bot_session.get_backtest_report(session, report_id),
            {
                "detail": "detail",
                "export_path": "legacy.zip",
                "export_spec": export_spec,
            },
        )

    def test_export_only_report_gets_an_id_and_fallback_keeps_spec(self):
        export_spec = {"version": 1}
        session = {}

        report_id = app_bot_session.store_backtest_report(
            session,
            None,
            export_spec=export_spec,
        )

        self.assertEqual(len(report_id), 12)
        self.assertEqual(
            app_bot_session.get_backtest_report(session),
            {
                "detail": None,
                "export_path": None,
                "export_spec": export_spec,
            },
        )

        legacy_session = {
            "last_backtest_detail": "legacy detail",
            "last_backtest_csv_path": "legacy.zip",
            "last_backtest_export_spec": export_spec,
        }
        self.assertEqual(
            app_bot_session.get_backtest_report(legacy_session)["export_spec"],
            export_spec,
        )

    def test_export_spec_is_frozen_and_getter_returns_a_fresh_copy(self):
        session = {}
        export_spec = {"version": 1, "trades": [{"ticker": "AAA"}]}
        report_id = app_bot_session.store_backtest_report(
            session,
            "detail",
            export_spec=export_spec,
        )

        export_spec["trades"][0]["ticker"] = "MUTATED"
        first_read = app_bot_session.get_backtest_report(session, report_id)
        first_read["export_spec"]["trades"][0]["ticker"] = "CHANGED_AGAIN"
        second_read = app_bot_session.get_backtest_report(session, report_id)

        self.assertEqual(
            second_read["export_spec"]["trades"][0]["ticker"],
            "AAA",
        )
        self.assertIn("_export_blob", session["backtest_reports"][report_id])
        self.assertNotIn("export_spec", session["backtest_reports"][report_id])

    def test_expired_report_is_pruned(self):
        session = {}
        with mock.patch.object(app_bot_session.time, "time", return_value=100.0):
            report_id = app_bot_session.store_backtest_report(
                session,
                "detail",
                export_spec={"version": 1},
            )

        with mock.patch.object(
            app_bot_session.time,
            "time",
            return_value=100.0 + app_bot_session.BACKTEST_REPORT_TTL_SECONDS + 1,
        ):
            self.assertIsNone(
                app_bot_session.get_backtest_report(session, report_id)
            )

    def test_only_eight_report_payloads_are_retained(self):
        session = {}
        report_ids = [
            app_bot_session.store_backtest_report(
                session,
                f"detail {index}",
                export_spec={"index": index},
            )
            for index in range(app_bot_session.MAX_BACKTEST_REPORTS_PER_CHAT + 1)
        ]

        self.assertEqual(
            len(session["backtest_reports"]),
            app_bot_session.MAX_BACKTEST_REPORTS_PER_CHAT,
        )
        self.assertIsNone(
            app_bot_session.get_backtest_report(session, report_ids[0])
        )
        self.assertEqual(
            app_bot_session.get_backtest_report(session, report_ids[-1])[
                "export_spec"
            ],
            {"index": app_bot_session.MAX_BACKTEST_REPORTS_PER_CHAT},
        )

    def test_derived_universe_config_is_not_reported_as_synced(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.py"
            original = (
                "universe_groups = {'A': ['AAA']}\n"
                "universe = sorted(set(universe_groups['A']))\n"
            )
            config_path.write_text(original, encoding="utf-8")

            synced = app_bot_session.sync_config_file(directory)

            self.assertFalse(synced)
            self.assertEqual(
                config_path.read_text(encoding="utf-8"),
                original,
            )


if __name__ == "__main__":
    unittest.main()
