import unittest
from datetime import date

import pandas as pd

from catalyst_pit_overlay import PointInTimeCatalystOverlay
from run_catalyst_overlay_ab import _apply_fresh_long_tiebreaker


def _prices():
    index = pd.bdate_range("2025-01-01", periods=80)
    return pd.DataFrame({"Open": range(100, 180), "Close": range(101, 181)}, index=index)


class PointInTimeOverlayTests(unittest.TestCase):
    def test_author_outcome_is_not_visible_until_after_exit(self):
        prices = {"AAA": _prices()}
        records = [{"ticker": "AAA", "author": "a", "event_date": "2025-01-02", "is_estimate_up": True} for _ in range(10)]
        overlay = PointInTimeCatalystOverlay(records, prices)
        first = overlay.records[0]
        self.assertEqual(overlay.author_scores_on(first["exit_date"]), {})
        self.assertIn("a", overlay.author_scores_on(date(2025, 2, 5)))

    def test_7pct_and_turnover_are_excluded_but_high_is_diagnostic(self):
        prices = {"AAA": _prices()}
        records = [{"ticker": "AAA", "author": "a", "event_date": "2025-01-02", "is_estimate_up": True, "is_7pct_up": True, "is_52w_high": True}]
        overlay = PointInTimeCatalystOverlay(records, prices)
        factor = overlay.factor_on("AAA", date(2025, 1, 3))
        self.assertEqual(factor["catalyst_long_tier"], 0)
        self.assertTrue(factor["is_52w_high"])

    def test_completed_history_can_raise_a_later_estimate_up_event(self):
        prices = {"AAA": _prices()}
        records = (
            [{"ticker": "AAA", "author": "good", "event_date": "2025-01-02", "is_estimate_up": True}] * 10
            + [{"ticker": "AAA", "author": "bad", "event_date": "2025-01-02", "is_estimate_up": False}] * 10
            + [{"ticker": "AAA", "author": "good", "event_date": "2025-02-10", "is_estimate_up": True}]
        )
        overlay = PointInTimeCatalystOverlay(records, prices)
        factor = overlay.factor_on("AAA", date(2025, 2, 11))
        self.assertEqual(factor["catalyst_long_tier"], 2)

    def test_linear_decay_uses_trading_days_and_new_signal_resets_it(self):
        prices = {"AAA": _prices()}
        dates = prices["AAA"].index
        records = [
            {"ticker": "AAA", "author": "a", "event_date": dates[1], "is_estimate_up": True},
            {"ticker": "AAA", "author": "a", "event_date": dates[5], "is_estimate_up": True},
        ]
        overlay = PointInTimeCatalystOverlay(records, prices)
        first_day = overlay.factor_on("AAA", dates[1].date())
        next_day = overlay.factor_on("AAA", dates[2].date())
        reset_day = overlay.factor_on("AAA", dates[5].date())
        expired = overlay.factor_on("AAA", dates[25].date())
        self.assertEqual(first_day["catalyst_age_trading_days"], 0)
        self.assertAlmostEqual(first_day["catalyst_decay"], 1.0)
        self.assertAlmostEqual(next_day["catalyst_decay"], 19 / 20)
        self.assertEqual(reset_day["catalyst_event_date"], dates[5].date().isoformat())
        self.assertAlmostEqual(reset_day["catalyst_decay"], 1.0)
        self.assertEqual(expired["catalyst_long_tier"], 0)

    def test_fresh_tier2_can_only_replace_a_name_just_below_the_cutoff(self):
        ranked = [
            {"ticker": "A", "fresh_tier2": False, "catalyst_age_trading_days": None},
            {"ticker": "B", "fresh_tier2": False, "catalyst_age_trading_days": None},
            {"ticker": "C", "fresh_tier2": True, "catalyst_age_trading_days": 2},
            {"ticker": "D", "fresh_tier2": True, "catalyst_age_trading_days": 1},
        ]
        adjusted = _apply_fresh_long_tiebreaker(ranked, slots=2, fresh_days=3, max_rank_gap=1)
        self.assertEqual([row["ticker"] for row in adjusted[:2]], ["A", "C"])


if __name__ == "__main__":
    unittest.main()
