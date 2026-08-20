import importlib
import sys
import types
import unittest
from unittest.mock import patch


class DeploymentUiTests(unittest.TestCase):
    def test_admin_text_describes_deployment_local_storage(self):
        telebot = types.ModuleType("telebot")
        telebot_types = types.ModuleType("telebot.types")
        telebot_types.InlineKeyboardMarkup = object
        telebot_types.InlineKeyboardButton = object

        with patch.dict(
            sys.modules,
            {"telebot": telebot, "telebot.types": telebot_types},
        ):
            sys.modules.pop("app_bot_ui", None)
            app_bot_ui = importlib.import_module("app_bot_ui")

            text = app_bot_ui.admin_text()
            self.assertIn("QuantBot Deploy (build 2026.08.20)", text)
            self.assertIn(
                "Market cache, exports, and portfolio state are local to this deployment.",
                text,
            )
            self.assertNotIn("shared with Classic Beta", text)


if __name__ == "__main__":
    unittest.main()
