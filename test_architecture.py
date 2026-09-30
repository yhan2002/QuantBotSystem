from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

from tools.check_architecture import find_violations


class ArchitectureTest(unittest.TestCase):
    def test_preview_upload_includes_only_the_hidden_artifact_directory(self):
        workflow = (ROOT / '.github/workflows/architecture.yml').read_text(encoding='utf-8')
        upload = workflow.split('uses: actions/upload-artifact@v4', 1)[1]
        self.assertIn('path: .agent-artifacts/', upload)
        self.assertIn('include-hidden-files: true', upload)
        self.assertIn('if-no-files-found: error', upload)

    def test_all_versions_follow_dependency_contract(self) -> None:
        self.assertEqual([], find_violations())


if __name__ == "__main__":
    unittest.main()
