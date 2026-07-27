import tempfile
import unittest
from pathlib import Path

import app_bot_session


class BotSessionTests(unittest.TestCase):
    def tearDown(self):
        app_bot_session.USER_SESSIONS.clear()

    def test_removed_strategy_falls_back_to_momentum(self):
        app_bot_session.USER_SESSIONS["legacy-chat"] = {
            "strategy": "REMOVED_STRATEGY",
        }

        session = app_bot_session.get_or_create_session("legacy-chat")

        self.assertEqual(session["strategy"], "MA_CROSS")

    def test_report_ids_keep_results_independent(self):
        session = {}
        first = app_bot_session.store_backtest_report(
            session, "first detail", "first.zip"
        )
        second = app_bot_session.store_backtest_report(
            session, "second detail", "second.zip"
        )

        self.assertNotEqual(first, second)
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
