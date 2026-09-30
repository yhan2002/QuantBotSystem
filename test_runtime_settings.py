"""Resolve runtime DB paths without importing bots, credentials or data stores."""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
VERSIONS = ("QuantBotSystem", "QuantBotSystem_Portfolio_Beta")


def load_settings(version, overrides):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("QUANTBOT_")}
    environment.update(overrides)
    spec = importlib.util.spec_from_file_location(
        "_isolated_runtime_settings", ROOT / version / "runtime_settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True), patch.dict(
        sys.modules, {"config": ModuleType("config")}
    ):
        spec.loader.exec_module(module)
    return module


class RuntimeMarketDBPathTests(unittest.TestCase):
    def default_path(self, version):
        folder = version if version == "QuantBotSystem" else "QuantBotSystem_Beta"
        return (ROOT / folder / "data" / "market_data.sqlite").resolve()

    def test_absent_path_uses_established_default(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                self.assertEqual(load_settings(version, {}).MARKET_DB_PATH, self.default_path(version))

    def test_blank_and_whitespace_path_use_established_default(self):
        for version in VERSIONS:
            for value in ("", " ", "\t\n", " \t \r\n "):
                with self.subTest(version=version, value=repr(value)):
                    module = load_settings(version, {"QUANTBOT_MARKET_DB_PATH": value})
                    self.assertEqual(module.MARKET_DB_PATH, self.default_path(version))

    def test_explicit_paths_preserve_existing_resolution(self):
        for version in VERSIONS:
            for value in ("custom.sqlite", "data/custom database.sqlite", " spaced.sqlite "):
                with self.subTest(version=version, value=value):
                    module = load_settings(version, {"QUANTBOT_MARKET_DB_PATH": value})
                    self.assertEqual(module.MARKET_DB_PATH, Path(value).resolve())

    def test_explicit_path_keeps_environment_expansion(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                module = load_settings(version, {
                    "QUANTBOT_MARKET_DB_PATH": "$QUANTBOT_TEST_DIR/custom.sqlite",
                    "QUANTBOT_TEST_DIR": str(ROOT / "isolated test paths"),
                })
                self.assertEqual(module.MARKET_DB_PATH, (ROOT / "isolated test paths/custom.sqlite").resolve())

    def test_default_respects_deploy_data_override_and_beta_shared_db(self):
        custom_data = ROOT / "isolated custom data"
        for version in VERSIONS:
            for value in (None, "", " \t "):
                with self.subTest(version=version, value=value):
                    overrides = {"QUANTBOT_DATA_DIR": str(custom_data)}
                    if value is not None:
                        overrides["QUANTBOT_MARKET_DB_PATH"] = value
                    module = load_settings(version, overrides)
                    expected = custom_data / "market_data.sqlite" if version == "QuantBotSystem" else self.default_path(version)
                    self.assertEqual(module.MARKET_DB_PATH, expected.resolve())


if __name__ == "__main__":
    unittest.main()
