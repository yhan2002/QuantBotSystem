import unittest

import pandas as pd

from core_portfolio_allocator import PortfolioPolicy
from core_portfolio_ledger import simulate_portfolio


def _prices(values):
    index = pd.to_datetime([row[0] for row in values])
    return pd.DataFrame(
        {
            "Open": [row[1] for row in values],
            "High": [max(row[1], row[2]) for row in values],
            "Low": [min(row[1], row[2]) for row in values],
            "Close": [row[2] for row in values],
            "Volume": 1000,
        },
        index=index,
    )


class PortfolioLedgerTests(unittest.TestCase):
    def setUp(self):
        self.frames = {
            "AAA": _prices(
                [
                    ("2026-01-02", 100, 100),
                    ("2026-01-05", 110, 110),
                    ("2026-01-06", 120, 120),
                ]
            ),
            "BBB": _prices(
                [
                    ("2026-01-02", 50, 50),
                    ("2026-01-05", 45, 45),
                    ("2026-01-06", 45, 45),
                ]
            ),
        }

    def loader(self, ticker):
        return self.frames[ticker]

    def test_five_percent_long_turns_ten_percent_trade_gain_into_half_percent_nav(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100,
            "signal_score": 2.0,
            "return": 10.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 110,
                    "reason": "exit",
                }
            ],
        }
        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
        )
        self.assertAlmostEqual(result["daily"].iloc[-1]["portfolio_nav"], 100.5)
        self.assertAlmostEqual(
            result["trades"][0]["entry_weight_pct"], 5.0
        )

    def test_partial_exits_reduce_original_quantity_without_reinvestment(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100,
            "signal_score": 1.0,
            "return": 15.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 0.5,
                    "price": 110,
                    "reason": "partial",
                },
                {
                    "date": "2026-01-06",
                    "weight": 0.5,
                    "price": 120,
                    "reason": "final",
                },
            ],
        }
        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-06",
            price_loader=self.loader,
        )
        exits = [
            event for event in result["events"] if event["event"] == "EXIT"
        ]
        self.assertEqual(len(exits), 2)
        self.assertAlmostEqual(exits[0]["quantity"], 0.025)
        self.assertAlmostEqual(exits[1]["quantity"], 0.025)
        self.assertAlmostEqual(result["daily"].iloc[-1]["portfolio_nav"], 100.75)

    def test_short_sale_cash_and_nav_have_correct_sign(self):
        trade = {
            "ticker": "BBB",
            "side": "SHORT",
            "entry_date": "2026-01-02",
            "entry_price": 50,
            "signal_score": -2.0,
            "return": 10.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 45,
                    "reason": "exit",
                }
            ],
        }
        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
        )
        self.assertGreater(result["daily"].iloc[0]["cash"], 100.0)
        self.assertAlmostEqual(result["daily"].iloc[-1]["portfolio_nav"], 100.5)

    def test_multi_leg_trade_counts_alpha_and_hedge_in_gross(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "signal_score": 3.0,
            "return": 0.0,
            "legs": [
                {
                    "ticker": "AAA",
                    "signed_ratio": 1.0,
                    "entry_price": 100,
                    "role": "ALPHA",
                },
                {
                    "ticker": "BBB",
                    "signed_ratio": -0.8,
                    "entry_price": 50,
                    "role": "HEDGE",
                },
            ],
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "execution": "NEXT_OPEN",
                    "reason": "residual mean",
                }
            ],
        }
        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
        )
        allocation = result["trades"][0]
        self.assertAlmostEqual(allocation["entry_weight_pct"], 5.0)
        self.assertAlmostEqual(allocation["allocated_gross_notional"], 9.0)
        self.assertAlmostEqual(
            result["daily"].iloc[0]["gross_exposure_pct"], 9.0
        )

    def test_same_day_exit_capacity_is_not_reused_at_that_open(self):
        policy = PortfolioPolicy(
            max_gross=1.0,
            max_name_weight=1.0,
            position_mode="LONG_ONLY",
        )
        trades = [
            {
                "ticker": "AAA",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "entry_price": 100,
                "signal_score": 2.0,
                "return": 10.0,
                "exit_events": [
                    {
                        "date": "2026-01-05",
                        "weight": 1.0,
                        "price": 110,
                        "reason": "exit",
                    }
                ],
            },
            {
                "ticker": "BBB",
                "side": "LONG",
                "entry_date": "2026-01-05",
                "entry_price": 45,
                "signal_score": 2.0,
                "return": 0.0,
                "exit_events": [
                    {
                        "date": "2026-01-06",
                        "weight": 1.0,
                        "price": 45,
                        "reason": "exit",
                    }
                ],
            },
        ]
        result = simulate_portfolio(
            trades,
            "2026-01-02",
            "2026-01-06",
            price_loader=self.loader,
            policy=policy,
        )
        statuses = {
            trade["ticker"]: trade["allocation_status"]
            for trade in result["trades"]
        }
        self.assertEqual(statuses["AAA"], "FILLED")
        self.assertEqual(statuses["BBB"], "SKIPPED_CAPACITY")

    def test_failed_second_leg_does_not_consume_cohort_capacity(self):
        dates = self.frames["AAA"].index
        self.frames["MISS"] = pd.DataFrame(
            {"Close": [25.0, 25.0, 25.0]},
            index=dates,
        )
        policy = PortfolioPolicy(
            max_gross=0.05,
            max_name_weight=1.0,
            position_mode="LONG_ONLY",
        )
        trades = [
            {
                "ticker": "AAA",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "entry_price": 100.0,
                "signal_score": 2.0,
                "return": 0.0,
                "exit_events": [
                    {
                        "date": "2026-01-05",
                        "weight": 1.0,
                        "price": 100.0,
                    }
                ],
            },
            {
                "ticker": "BBB",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "signal_score": 1.0,
                "return": 0.0,
                "legs": [
                    {
                        "ticker": "BBB",
                        "signed_ratio": 1.0,
                        "entry_price": 50.0,
                    },
                    {
                        "ticker": "MISS",
                        "signed_ratio": 1.0,
                    },
                ],
                "exit_events": [
                    {"date": "2026-01-05", "weight": 1.0}
                ],
            },
        ]

        result = simulate_portfolio(
            trades,
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            policy=policy,
        )
        allocation = {row["ticker"]: row for row in result["trades"]}

        self.assertEqual(allocation["BBB"]["allocation_status"], "SKIPPED_MISSING_OPEN")
        self.assertEqual(allocation["AAA"]["allocation_status"], "FILLED")
        self.assertAlmostEqual(allocation["AAA"]["allocated_base_notional"], 5.0)
        self.assertAlmostEqual(
            result["daily"].iloc[0]["gross_exposure_pct"],
            5.0,
        )

    def test_binding_gross_cap_is_independent_of_trade_order(self):
        policy = PortfolioPolicy(
            max_gross=0.05,
            max_name_weight=1.0,
            position_mode="LONG_ONLY",
        )
        trades = [
            {
                "ticker": "AAA",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "entry_price": 100.0,
                "signal_score": 2.0,
                "return": 10.0,
                "exit_events": [
                    {
                        "date": "2026-01-05",
                        "weight": 1.0,
                        "price": 110.0,
                    }
                ],
            },
            {
                "ticker": "BBB",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "entry_price": 50.0,
                "signal_score": 1.0,
                "return": -10.0,
                "exit_events": [
                    {
                        "date": "2026-01-05",
                        "weight": 1.0,
                        "price": 45.0,
                    }
                ],
            },
        ]

        forward = simulate_portfolio(
            trades,
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            policy=policy,
        )
        reverse = simulate_portfolio(
            list(reversed(trades)),
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            policy=policy,
        )
        forward_allocations = {
            row["ticker"]: row["allocated_base_notional"]
            for row in forward["trades"]
        }
        reverse_allocations = {
            row["ticker"]: row["allocated_base_notional"]
            for row in reverse["trades"]
        }

        self.assertEqual(forward_allocations, reverse_allocations)
        self.assertAlmostEqual(sum(forward_allocations.values()), 5.0)
        self.assertAlmostEqual(
            forward["daily"].iloc[0]["gross_exposure_pct"],
            5.0,
        )

    def test_default_policy_matches_explicit_all_one_sizing(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100.0,
            "signal_score": 2.0,
            "return": 10.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 110.0,
                }
            ],
        }

        baseline = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
        )
        explicit = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            entry_sizing_policy=lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signal["trade_id"]: 1.0 for signal in signals
                },
            },
        )

        pd.testing.assert_frame_equal(baseline["daily"], explicit["daily"])
        self.assertEqual(
            baseline["trades"][0]["allocated_base_notional"],
            explicit["trades"][0]["allocated_base_notional"],
        )
        self.assertAlmostEqual(
            baseline["trades"][0]["base_requested_weight"],
            baseline["trades"][0]["requested_weight"],
        )
        self.assertEqual(
            baseline["trades"][0]["policy_gross_scale"],
            1.0,
        )
        self.assertEqual(
            baseline["trades"][0]["policy_trade_multiplier"],
            1.0,
        )

    def test_gross_and_trade_scales_multiply_before_existing_caps(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100.0,
            "signal_score": 2.0,
            "return": 10.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 110.0,
                }
            ],
        }

        half = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            entry_sizing_policy=lambda signals, portfolio_state: {
                "gross_scale": 0.5,
                "trade_multipliers": {
                    signal["trade_id"]: 1.0 for signal in signals
                },
            },
        )
        combined = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            entry_sizing_policy=lambda signals, portfolio_state: {
                "gross_scale": 0.5,
                "trade_multipliers": {
                    signal["trade_id"]: 0.5 for signal in signals
                },
            },
        )

        self.assertAlmostEqual(
            half["trades"][0]["entry_weight_pct"],
            2.5,
        )
        allocation = combined["trades"][0]
        self.assertAlmostEqual(allocation["base_requested_weight"], 0.05)
        self.assertAlmostEqual(allocation["policy_gross_scale"], 0.5)
        self.assertAlmostEqual(allocation["policy_trade_multiplier"], 0.5)
        self.assertAlmostEqual(allocation["requested_weight"], 0.0125)
        self.assertAlmostEqual(allocation["entry_weight_pct"], 1.25)
        self.assertAlmostEqual(
            combined["daily"].iloc[-1]["portfolio_nav"],
            100.125,
        )

    def test_entry_sizing_policy_requires_exact_valid_cohort_decision(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100.0,
            "signal_score": 2.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 110.0,
                }
            ],
        }

        invalid_policies = {
            "missing id": lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {},
            },
            "extra id": lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signals[0]["trade_id"]: 1.0,
                    "EXTRA": 1.0,
                },
            },
            "invalid gross": lambda signals, portfolio_state: {
                "gross_scale": float("nan"),
                "trade_multipliers": {
                    signals[0]["trade_id"]: 1.0,
                },
            },
            "invalid multiplier": lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signals[0]["trade_id"]: -0.01,
                },
            },
        }

        for label, sizing_policy in invalid_policies.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                simulate_portfolio(
                    [trade],
                    "2026-01-02",
                    "2026-01-05",
                    price_loader=self.loader,
                    entry_sizing_policy=sizing_policy,
                )

    def test_entry_sizing_policy_allows_upweight_but_name_cap_still_applies(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "entry_price": 100.0,
            "signal_score": 2.0,
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "price": 110.0,
                }
            ],
        }

        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            entry_sizing_policy=lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signals[0]["trade_id"]: 1.5,
                },
            },
        )

        allocation = result["trades"][0]
        self.assertEqual(allocation["policy_trade_multiplier"], 1.5)
        self.assertAlmostEqual(allocation["requested_weight"], 0.075)
        self.assertAlmostEqual(allocation["entry_weight_pct"], 5.0)

    def test_policy_sees_only_sanitized_signal_and_prior_close_state(self):
        calls = []
        trades = [
            {
                "strategy": "MOMENTUM",
                "ticker": "AAA",
                "side": "LONG",
                "signal_date": "2026-01-01",
                "entry_date": "2026-01-02",
                "entry_price": 100.0,
                "next_open": 100.0,
                "gap": 0.03,
                "signal_score": 2.0,
                "return": 20.0,
                "exit_price": 120.0,
                "setup": "alignment",
                "structure": {"trend": "up"},
                "policy_features": {"volatility": 0.2},
                "exit_events": [
                    {
                        "date": "2026-01-06",
                        "weight": 1.0,
                        "price": 120.0,
                    }
                ],
            },
            {
                "strategy": "MOMENTUM",
                "ticker": "BBB",
                "side": "LONG",
                "signal_date": "2026-01-02",
                "entry_date": "2026-01-05",
                "entry_price": 45.0,
                "next_open": 45.0,
                "gap": -0.04,
                "signal_score": 1.0,
                "return": 0.0,
                "exit_price": 45.0,
                "exit_events": [
                    {
                        "date": "2026-01-06",
                        "weight": 1.0,
                        "price": 45.0,
                    }
                ],
            },
        ]

        def sizing_policy(*, signals, portfolio_state):
            calls.append((signals, portfolio_state))
            return {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signal["trade_id"]: 1.0 for signal in signals
                },
            }

        simulate_portfolio(
            trades,
            "2026-01-02",
            "2026-01-06",
            price_loader=self.loader,
            entry_sizing_policy=sizing_policy,
        )

        self.assertEqual(len(calls), 2)
        exposed_signal = calls[0][0][0]
        forbidden = {
            "return",
            "exit_events",
            "exit_price",
            "entry_price",
            "next_open",
            "gap",
            "legs",
        }
        self.assertTrue(forbidden.isdisjoint(exposed_signal))
        self.assertTrue(
            set(exposed_signal).issubset(
                {
                    "trade_id",
                    "strategy",
                    "ticker",
                    "side",
                    "signal_date",
                    "entry_date",
                    "signal_score",
                    "neutralized_score",
                    "selected_score",
                    "winsorized_score",
                    "percentile_rank",
                    "rank_score",
                    "requested_weight",
                    "setup",
                    "structure",
                    "policy_features",
                }
            )
        )
        self.assertEqual(exposed_signal["setup"], "alignment")
        self.assertEqual(
            exposed_signal["policy_features"],
            {"volatility": 0.2},
        )

        first_state = calls[0][1]
        second_state = calls[1][1]
        self.assertIsNone(first_state["asof_date"])
        self.assertEqual(second_state["asof_date"], "2026-01-02")
        self.assertAlmostEqual(second_state["nav"], 100.0)
        self.assertAlmostEqual(second_state["cash"], 95.0)
        self.assertAlmostEqual(second_state["long_fraction"], 0.05)
        self.assertAlmostEqual(second_state["gross_fraction"], 0.05)
        self.assertEqual(second_state["active_positions"], 1)
        self.assertEqual(
            set(second_state),
            {
                "asof_date",
                "nav",
                "cash",
                "long_fraction",
                "short_fraction",
                "gross_fraction",
                "net_fraction",
                "active_positions",
            },
        )

    def test_multi_leg_trade_uses_one_uniform_policy_multiplier(self):
        trade = {
            "ticker": "AAA",
            "side": "LONG",
            "entry_date": "2026-01-02",
            "signal_score": 3.0,
            "legs": [
                {
                    "ticker": "AAA",
                    "signed_ratio": 1.0,
                    "entry_price": 100.0,
                    "role": "ALPHA",
                },
                {
                    "ticker": "BBB",
                    "signed_ratio": -0.8,
                    "entry_price": 50.0,
                    "role": "HEDGE",
                },
            ],
            "exit_events": [
                {
                    "date": "2026-01-05",
                    "weight": 1.0,
                    "execution": "NEXT_OPEN",
                }
            ],
        }
        result = simulate_portfolio(
            [trade],
            "2026-01-02",
            "2026-01-05",
            price_loader=self.loader,
            entry_sizing_policy=lambda signals, portfolio_state: {
                "gross_scale": 1.0,
                "trade_multipliers": {
                    signals[0]["trade_id"]: 0.5,
                },
            },
        )

        allocation = result["trades"][0]
        entries = [
            event for event in result["events"] if event["event"] == "ENTRY"
        ]
        notionals = {event["role"]: event["notional"] for event in entries}
        self.assertAlmostEqual(allocation["entry_weight_pct"], 2.5)
        self.assertAlmostEqual(allocation["allocated_gross_notional"], 4.5)
        self.assertAlmostEqual(notionals["ALPHA"], 2.5)
        self.assertAlmostEqual(notionals["HEDGE"], 2.0)

    def test_trade_ids_must_be_globally_unique_across_entry_dates(self):
        trades = [
            {
                "trade_id": "DUPLICATE",
                "ticker": "AAA",
                "side": "LONG",
                "entry_date": "2026-01-02",
                "signal_score": 1.0,
                "exit_date": "2026-01-05",
                "exit_price": 110.0,
            },
            {
                "trade_id": "DUPLICATE",
                "ticker": "BBB",
                "side": "SHORT",
                "entry_date": "2026-01-05",
                "signal_score": -1.0,
                "exit_date": "2026-01-06",
                "exit_price": 45.0,
            },
        ]

        with self.assertRaisesRegex(ValueError, "globally unique trade IDs"):
            simulate_portfolio(
                trades,
                "2026-01-02",
                "2026-01-06",
                price_loader=self.loader,
            )


if __name__ == "__main__":
    unittest.main()
