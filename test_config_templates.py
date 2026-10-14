"""Blank templates and ignored settings; no bots, API calls or live settings."""
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.prepare_local_config import ROOT, VERSIONS, prepare


class ConfigTemplateTests(unittest.TestCase):
    def test_fresh_copy_imports_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in VERSIONS:
                target = root / version
                target.mkdir()
                (target / "config.local.example.py").write_bytes(
                    (ROOT / version / "config.local.example.py").read_bytes()
                )
            with redirect_stdout(StringIO()):
                prepare(root)
            for version in VERSIONS:
                target = root / version / "config.py"
                spec = importlib.util.spec_from_file_location("_blank_config", target)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(module.BOT_TOKEN == "", "Template token must be blank")
                self.assertTrue(module.ALLOWED_CHAT_IDS == [], "Template recipients must be empty")
                self.assertTrue(module.SCALPING_WORKBOOK_PATH == "", "Template private path must be blank")
                self.assertTrue(module.universe_groups)
                self.assertEqual(module.universe, sorted(set().union(*module.universe_groups.values())))
                self.assertEqual("0005.HK", module.normalize_ticker("5.hk"))
                target.write_bytes(b"# Existing local settings must remain byte-for-byte intact.\n")
            with redirect_stdout(StringIO()):
                prepare(root)
            for version in VERSIONS:
                self.assertEqual((root / version / "config.py").read_bytes(),
                                 b"# Existing local settings must remain byte-for-byte intact.\n")

    def test_private_config_paths_are_ignored_and_untracked(self):
        for version in VERSIONS:
            path = f"{version}/config.py"
            tracked = subprocess.check_output(["git", "ls-files", "--", path], cwd=ROOT)
            self.assertFalse(tracked, "Local config must not be tracked")
            ignored = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=ROOT)
            self.assertEqual(0, ignored.returncode)


if __name__ == "__main__":
    unittest.main()
