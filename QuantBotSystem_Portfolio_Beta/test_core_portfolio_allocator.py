import math
import unittest

from core_portfolio_allocator import PortfolioPolicy, cohort_capacity_scale


class PortfolioPolicyTests(unittest.TestCase):
    def test_every_numeric_policy_field_must_be_finite(self):
        fields = (
            "initial_nav",
            "max_gross",
            "max_name_weight",
            "long_cap",
            "short_cap",
        )
        for field in fields:
            for invalid in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        PortfolioPolicy(**{field: invalid})

    def test_numeric_policy_values_are_normalized_to_float(self):
        policy = PortfolioPolicy(
            initial_nav="100",
            max_gross="1",
            max_name_weight="0.05",
            long_cap="0.6",
            short_cap="0.4",
        )

        for field in (
            "initial_nav",
            "max_gross",
            "max_name_weight",
            "long_cap",
            "short_cap",
        ):
            self.assertIsInstance(getattr(policy, field), float)
            self.assertTrue(math.isfinite(getattr(policy, field)))
        self.assertEqual(policy.max_positions, 30)

    def test_max_positions_must_be_a_positive_integer(self):
        for invalid in (0, -1, 1.5, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    PortfolioPolicy(max_positions=invalid)
        self.assertEqual(PortfolioPolicy(max_positions="30").max_positions, 30)


class CohortCapacityTests(unittest.TestCase):
    def test_gross_cap_binds_independently_of_leg_order(self):
        policy = PortfolioPolicy(
            max_gross=0.20,
            max_name_weight=1.0,
            position_mode="LONG_ONLY",
        )
        legs = [
            {"ticker": "AAA", "notional": 12.0, "direction": 1.0},
            {"ticker": "BBB", "notional": 12.0, "direction": 1.0},
        ]

        forward = cohort_capacity_scale(
            legs,
            nav=100.0,
            current_long=5.0,
            current_short=0.0,
            current_name_gross={},
            policy=policy,
        )
        reverse = cohort_capacity_scale(
            list(reversed(legs)),
            nav=100.0,
            current_long=5.0,
            current_short=0.0,
            current_name_gross={},
            policy=policy,
        )

        self.assertAlmostEqual(forward, 15.0 / 24.0)
        self.assertAlmostEqual(reverse, forward)
        self.assertAlmostEqual(5.0 + 24.0 * forward, 20.0)

    def test_name_cap_aggregates_same_ticker_legs_and_is_order_independent(self):
        policy = PortfolioPolicy(
            max_gross=1.0,
            max_name_weight=0.05,
            position_mode="LONG_ONLY",
        )
        legs = [
            {"ticker": "AAA", "notional": 4.0, "direction": 1.0},
            {"ticker": "BBB", "notional": 2.0, "direction": 1.0},
            {"ticker": "AAA", "notional": 4.0, "direction": 1.0},
        ]

        forward = cohort_capacity_scale(
            legs,
            nav=100.0,
            current_long=0.0,
            current_short=0.0,
            current_name_gross={},
            policy=policy,
        )
        reverse = cohort_capacity_scale(
            list(reversed(legs)),
            nav=100.0,
            current_long=0.0,
            current_short=0.0,
            current_name_gross={},
            policy=policy,
        )

        self.assertAlmostEqual(forward, 5.0 / 8.0)
        self.assertAlmostEqual(reverse, forward)
        self.assertAlmostEqual((4.0 + 4.0) * forward, 5.0)


if __name__ == "__main__":
    unittest.main()
