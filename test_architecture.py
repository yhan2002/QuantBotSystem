from __future__ import annotations

import unittest

from tools.check_architecture import find_violations


class ArchitectureTest(unittest.TestCase):
    def test_all_versions_follow_dependency_contract(self) -> None:
        self.assertEqual([], find_violations())


if __name__ == "__main__":
    unittest.main()
