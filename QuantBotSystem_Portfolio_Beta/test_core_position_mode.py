import unittest

import core_position_mode


class PositionModeTests(unittest.TestCase):
    def test_side_filters_match_modes(self):
        self.assertEqual(
            core_position_mode.side_filter(core_position_mode.LONG_ONLY), "LONG"
        )
        self.assertIsNone(
            core_position_mode.side_filter(core_position_mode.LONG_SHORT_NEUTRAL)
        )
        self.assertEqual(
            core_position_mode.side_filter(core_position_mode.SHORT_ONLY), "SHORT"
        )

    def test_direction_permissions_match_modes(self):
        self.assertTrue(core_position_mode.allows_long(core_position_mode.LONG_ONLY))
        self.assertFalse(core_position_mode.allows_short(core_position_mode.LONG_ONLY))
        self.assertTrue(
            core_position_mode.allows_long(core_position_mode.LONG_SHORT_NEUTRAL)
        )
        self.assertTrue(
            core_position_mode.allows_short(core_position_mode.LONG_SHORT_NEUTRAL)
        )
        self.assertFalse(core_position_mode.allows_long(core_position_mode.SHORT_ONLY))
        self.assertTrue(core_position_mode.allows_short(core_position_mode.SHORT_ONLY))


if __name__ == "__main__":
    unittest.main()
