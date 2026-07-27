import unittest

import app_bot_session
import app_bot_ui


def _button_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


def _callback_data(markup):
    return [button.callback_data for row in markup.keyboard for button in row]


class BotUiTests(unittest.TestCase):
    def setUp(self):
        app_bot_session.USER_SESSIONS.clear()

    def test_home_removes_box_tp_sl_button(self):
        texts = _button_texts(app_bot_ui.build_home_markup())
        self.assertNotIn("Box TP/SL Test", texts)
        self.assertIn("Quick Ticker Check", texts)

    def test_settings_exposes_neutralization_modes(self):
        chat_id = "test-chat"
        settings_texts = _button_texts(app_bot_ui.build_settings_markup(chat_id))
        neutralization_texts = _button_texts(
            app_bot_ui.build_neutralization_markup(chat_id)
        )

        self.assertIn("Neutralize", settings_texts)
        self.assertIn("[x] None", neutralization_texts)
        self.assertIn("[ ] Market", neutralization_texts)
        self.assertIn("[ ] Country", neutralization_texts)
        self.assertIn("[ ] Sector", neutralization_texts)
        self.assertIn("[ ] Industry", neutralization_texts)

    def test_settings_exposes_position_modes(self):
        chat_id = "test-chat"
        settings_texts = _button_texts(app_bot_ui.build_settings_markup(chat_id))
        position_texts = _button_texts(
            app_bot_ui.build_position_mode_markup(chat_id)
        )

        self.assertIn("Position Mode", settings_texts)
        self.assertIn("[ ] Long Only", position_texts)
        self.assertIn("[x] Long / Short Neutral", position_texts)
        self.assertIn("[ ] Short Only", position_texts)
        self.assertIn("Position: Long / Short Neutral", app_bot_ui.settings_text(chat_id))

    def test_strategy_names_are_consistent(self):
        chat_id = "test-chat"
        strategy_texts = _button_texts(app_bot_ui.build_strategy_markup(chat_id))

        self.assertEqual(
            strategy_texts[:-1],
            [
                "[x] Momentum",
                "[ ] Box Range",
                "[ ] Reversion",
            ],
        )
        self.assertIn("Strategy: Momentum", app_bot_ui.settings_text(chat_id))

    def test_admin_separates_latest_update_from_full_backfill(self):
        markup = app_bot_ui.build_admin_markup()
        texts = _button_texts(markup)
        callbacks = _callback_data(markup)

        self.assertIn("Update Selected (Latest)", texts)
        self.assertIn("Update All (Latest)", texts)
        self.assertIn("Backfill Selected (4Y)", texts)
        self.assertIn("Backfill All (4Y)", texts)
        self.assertIn("admin_refresh_selected", callbacks)
        self.assertIn("admin_refresh_all", callbacks)
        self.assertIn("Refresh Benchmark", texts)
        self.assertIn("admin_refresh_benchmark", callbacks)
        self.assertNotIn("Preload 4Y Market Data", texts)
        self.assertIn("Portfolio Beta", app_bot_ui.admin_text())
        self.assertIn("Private config:", app_bot_ui.admin_text())
        self.assertIn("dividend-adjusted benchmark prices", app_bot_ui.admin_text())
        self.assertIn("Portfolio Beta", app_bot_ui.home_text("test-chat"))
        self.assertNotIn("PORTFOLIO_BETA", app_bot_ui.home_text("test-chat"))

    def test_admin_full_backfill_requires_explicit_confirmation(self):
        selected = app_bot_ui.build_admin_backfill_confirm_markup("selected")
        all_tickers = app_bot_ui.build_admin_backfill_confirm_markup("all")

        self.assertEqual(
            _callback_data(selected),
            ["admin_backfill_selected_run", "admin"],
        )
        self.assertEqual(
            _callback_data(all_tickers),
            ["admin_backfill_all_run", "admin"],
        )
        with self.assertRaises(ValueError):
            app_bot_ui.build_admin_backfill_confirm_markup("unknown")

    def test_see_detail_callback_is_tied_to_its_scan(self):
        markup = app_bot_ui.build_scan_result_markup(
            has_detail=True,
            report_id="abc123",
            has_export=True,
        )
        self.assertEqual(
            _callback_data(markup),
            ["scan_detail:abc123", "scan_export:abc123", "go_home"],
        )
        self.assertIn("Download Export", _button_texts(markup))

    def test_export_button_does_not_require_detail_button(self):
        markup = app_bot_ui.build_scan_result_markup(
            has_detail=False,
            report_id="export123",
            has_export=True,
        )

        self.assertEqual(
            _button_texts(markup),
            ["Download Export", "Back to Menu"],
        )
        self.assertEqual(
            _callback_data(markup),
            ["scan_export:export123", "go_home"],
        )


if __name__ == "__main__":
    unittest.main()
