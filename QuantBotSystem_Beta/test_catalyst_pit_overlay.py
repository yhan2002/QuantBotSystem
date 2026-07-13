import unittest
from datetime import date

import pandas as pd

from catalyst_pit_overlay import PointInTimeCatalystOverlay


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


if __name__ == "__main__":
    unittest.main()
