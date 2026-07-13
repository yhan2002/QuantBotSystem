import unittest

import ma_cross_engine as engine


class MomentumPitOverlayTests(unittest.TestCase):
    def test_tiebreaker_only_promotes_fresh_tier2_just_below_cutoff(self):
        rows = [
            {"ticker": "A", "catalyst_long_tier": 0, "catalyst_age_trading_days": None},
            {"ticker": "B", "catalyst_long_tier": 0, "catalyst_age_trading_days": None},
            {"ticker": "C", "catalyst_long_tier": 2, "catalyst_age_trading_days": 2},
        ]
        original_gap = engine.MOMENTUM_PIT_TIEBREAK_MAX_RANK_GAP
        try:
            engine.MOMENTUM_PIT_TIEBREAK_MAX_RANK_GAP = 1
            adjusted = engine._apply_pit_long_tiebreaker(rows, slots=2)
        finally:
            engine.MOMENTUM_PIT_TIEBREAK_MAX_RANK_GAP = original_gap
        self.assertEqual([row["ticker"] for row in adjusted[:2]], ["A", "C"])

    def test_tier2_outside_fresh_window_is_not_promoted(self):
        rows = [
            {"ticker": "A", "catalyst_long_tier": 0, "catalyst_age_trading_days": None},
            {"ticker": "B", "catalyst_long_tier": 0, "catalyst_age_trading_days": None},
            {"ticker": "C", "catalyst_long_tier": 2, "catalyst_age_trading_days": 11},
        ]
        self.assertEqual(engine._apply_pit_long_tiebreaker(rows, slots=2), rows)


if __name__ == "__main__":
    unittest.main()
