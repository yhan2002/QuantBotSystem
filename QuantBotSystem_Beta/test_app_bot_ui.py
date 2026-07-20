import unittest

import app_bot_session
import app_bot_ui


def _button_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


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
            ["[x] Momentum", "[ ] Box Range", "[ ] Reversion"],
        )
        self.assertIn("Strategy: Momentum", app_bot_ui.settings_text(chat_id))


if __name__ == "__main__":
    unittest.main()
