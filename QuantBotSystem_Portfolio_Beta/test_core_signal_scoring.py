import unittest
from dataclasses import FrozenInstanceError

from core_signal_model import Signal
import core_signal_scoring as subject


def _signal(ticker, side, raw_score, **kwargs):
    return Signal(
        strategy="TEST",
        ticker=ticker,
        side=side,
        signal_date="2026-07-23",
        raw_score=raw_score,
        **kwargs,
    )


class SignalModelTests(unittest.TestCase):
    def test_signal_is_normalized_and_immutable(self):
        metadata = {"sector": "Technology"}
        signal = Signal(
            strategy=" Test Strategy ",
            ticker="aapl",
            side="long",
            signal_date="2026-07-23",
            raw_score="1.25",
            status="READY",
            setup="PULLBACK",
            structure="RISING",
            metadata=metadata,
            entry_execution="NEXT_OPEN",
        )
        metadata["sector"] = "Changed"

        self.assertEqual(signal.strategy, "Test Strategy")
        self.assertEqual(signal.ticker, "AAPL")
        self.assertEqual(signal.side, "LONG")
        self.assertEqual(signal.raw_score, 1.25)
        self.assertEqual(signal.metadata["sector"], "Technology")
        with self.assertRaises(FrozenInstanceError):
            signal.ticker = "MSFT"
        with self.assertRaises(TypeError):
            signal.metadata["sector"] = "Changed"

    def test_signal_rejects_invalid_side_and_non_finite_score(self):
        with self.assertRaises(ValueError):
            _signal("AAA", "FLAT", 1.0)
        for invalid_score in (float("nan"), float("inf"), "bad"):
            with self.subTest(raw_score=invalid_score):
                with self.assertRaises(ValueError):
                    _signal("AAA", "LONG", invalid_score)
        for field_name in ("strategy", "ticker", "signal_date"):
            values = {
                "strategy": "TEST",
                "ticker": "AAA",
                "side": "LONG",
                "signal_date": "2026-07-23",
                "raw_score": 1.0,
            }
            values[field_name] = None
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    Signal(**values)


class SignalScoringTests(unittest.TestCase):
    def test_sign_inconsistent_and_zero_candidates_are_rejected(self):
        candidates = [
            (_signal("GOOD_LONG", "LONG", 1.0), 0.5),
            (_signal("BAD_LONG", "LONG", 1.0), -0.5),
            (_signal("ZERO_SHORT", "SHORT", -1.0), 0.0),
            (_signal("GOOD_SHORT", "SHORT", -1.0), -0.5),
        ]

        result = subject.score_signal_cohort(candidates)

        self.assertEqual(
            [row.signal.ticker for row in result],
            ["GOOD_LONG", "GOOD_SHORT"],
        )
        self.assertEqual(result[0].rank_score, 1.0)
        self.assertEqual(result[1].rank_score, -1.0)
        self.assertEqual(result[0].requested_weight, 0.05)

    def test_selected_score_must_be_finite(self):
        for selected_score in (float("nan"), float("inf"), "bad"):
            with self.subTest(selected_score=selected_score):
                with self.assertRaises(ValueError):
                    subject.score_signal_cohort(
                        [(_signal("AAA", "LONG", 1.0), selected_score)]
                    )

    def test_side_ranks_are_deterministic_with_average_ties(self):
        candidates = [
            (_signal("L_LOW", "LONG", 1.0), 1.0),
            (_signal("L_HIGH_A", "LONG", 3.0), 3.0),
            (_signal("L_HIGH_B", "LONG", 3.0), 3.0),
            (_signal("S_LOW", "SHORT", -2.0), -2.0),
            (_signal("S_HIGH", "SHORT", -4.0), -4.0),
        ]

        forward = subject.score_signal_cohort(candidates)
        reverse = subject.score_signal_cohort(reversed(candidates))
        forward_scores = {
            row.signal.ticker: (row.percentile_rank, row.rank_score)
            for row in forward
        }
        reverse_scores = {
            row.signal.ticker: (row.percentile_rank, row.rank_score)
            for row in reverse
        }

        self.assertEqual(forward_scores, reverse_scores)
        self.assertAlmostEqual(forward_scores["L_LOW"][0], 1.0 / 3.0)
        self.assertAlmostEqual(forward_scores["L_HIGH_A"][0], 2.5 / 3.0)
        self.assertEqual(
            forward_scores["L_HIGH_A"],
            forward_scores["L_HIGH_B"],
        )
        self.assertEqual(forward_scores["S_LOW"], (0.5, -0.5))
        self.assertEqual(forward_scores["S_HIGH"], (1.0, -1.0))

    def test_winsorization_is_applied_to_magnitude_per_side(self):
        candidates = [
            (_signal("L1", "LONG", 1.0), 1.0),
            (_signal("L2", "LONG", 3.0), 3.0),
            (_signal("L3", "LONG", 100.0), 100.0),
            (_signal("S1", "SHORT", -2.0), -2.0),
        ]

        result = subject.score_signal_cohort(
            candidates,
            winsor_limits=(0.0, 0.5),
        )
        by_ticker = {row.signal.ticker: row for row in result}

        self.assertEqual(by_ticker["L3"].winsorized_score, 3.0)
        self.assertEqual(by_ticker["L2"].winsorized_score, 3.0)
        self.assertEqual(by_ticker["S1"].winsorized_score, -2.0)
        self.assertAlmostEqual(by_ticker["L3"].percentile_rank, 2.5 / 3.0)

    def test_trade_annotation_groups_by_entry_date_and_keeps_context(self):
        trades = [
            {
                "strategy": "BOX",
                "ticker": "AAA",
                "side": "LONG",
                "entry_date": "2026-07-24",
                "signal_date": "2026-07-23",
                "signal_score": 1.0,
                "neutralized_score": 2.0,
                "setup": "RANGE_RECLAIM",
                "structure": "FLAT",
            },
            {
                "strategy": "BOX",
                "ticker": "BBB",
                "side": "LONG",
                "entry_date": "2026-07-24",
                "signal_score": 3.0,
                "setup": "PULLBACK",
                "structure": "RISING",
            },
            {
                "strategy": "BOX",
                "ticker": "REJECT",
                "side": "LONG",
                "entry_date": "2026-07-24",
                "signal_score": 5.0,
                "neutralized_score": -1.0,
            },
            {
                "strategy": "BOX",
                "ticker": "CCC",
                "side": "LONG",
                "entry_date": "2026-07-25",
                "signal_score": 0.5,
            },
        ]

        result = subject.annotate_trades_by_entry_date(trades)
        by_ticker = {row["ticker"]: row for row in result}

        self.assertEqual(set(by_ticker), {"AAA", "BBB", "CCC"})
        self.assertEqual(by_ticker["AAA"]["selected_score"], 2.0)
        self.assertEqual(by_ticker["BBB"]["selected_score"], 3.0)
        self.assertEqual(by_ticker["AAA"]["setup"], "RANGE_RECLAIM")
        self.assertEqual(by_ticker["AAA"]["structure"], "FLAT")
        self.assertEqual(by_ticker["CCC"]["rank_score"], 1.0)
        self.assertNotIn("requested_weight", trades[0])


if __name__ == "__main__":
    unittest.main()
