import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_settings


class RuntimeSettingsTests(unittest.TestCase):
    def _write_config(self, path, token):
        Path(path).write_text(
            f"BOT_TOKEN = {token!r}\nALLOWED_CHAT_IDS = {{'123'}}\n",
            encoding="utf-8",
        )

    def test_explicit_environment_config_has_highest_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            explicit_path = Path(temp_dir) / "explicit.py"
            local_path = Path(temp_dir) / "local.py"
            self._write_config(explicit_path, "explicit-token")
            self._write_config(local_path, "local-token")

            with patch.object(runtime_settings, "LOCAL_CONFIG_PATH", local_path), patch.dict(
                os.environ,
                {"QUANTBOT_CONFIG_PATH": str(explicit_path)},
                clear=False,
            ):
                self.assertEqual(
                    "explicit-token",
                    runtime_settings.get_setting("BOT_TOKEN"),
                )

    def test_local_config_is_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "local.py"
            self._write_config(local_path, "local-token")

            with patch.object(runtime_settings, "LOCAL_CONFIG_PATH", local_path), patch.dict(
                os.environ, {}, clear=False
            ):
                os.environ.pop("QUANTBOT_CONFIG_PATH", None)
                self.assertEqual(
                    "local-token",
                    runtime_settings.get_setting("BOT_TOKEN"),
                )

    def test_tracked_default_is_used_when_local_config_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "missing-local.py"
            with patch.object(runtime_settings, "LOCAL_CONFIG_PATH", local_path), patch.dict(
                os.environ, {}, clear=False
            ):
                os.environ.pop("QUANTBOT_CONFIG_PATH", None)
                self.assertEqual("", runtime_settings.get_setting("BOT_TOKEN"))

    def test_invalid_explicit_config_path_fails_clearly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.py"
            with patch.dict(
                os.environ,
                {"QUANTBOT_CONFIG_PATH": str(missing_path)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    FileNotFoundError, "QUANTBOT_CONFIG_PATH"
                ):
                    runtime_settings.resolve_config_path()

    def test_version_constants_are_explicit(self):
        self.assertEqual("DEPLOY", runtime_settings.VERSION)
        self.assertEqual("2026.08.20", runtime_settings.BUILD)
        self.assertEqual("QuantBot Deploy", runtime_settings.VERSION_DISPLAY_NAME)
        self.assertEqual(
            "QuantBot Deploy (build 2026.08.20)",
            runtime_settings.VERSION_LABEL,
        )

    def test_default_market_database_stays_in_deployment(self):
        self.assertEqual(
            runtime_settings.BASE_DIR / "data" / "market_data.sqlite",
            runtime_settings.MARKET_DB_PATH,
        )


if __name__ == "__main__":
    unittest.main()
