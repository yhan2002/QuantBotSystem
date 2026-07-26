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


if __name__ == "__main__":
    unittest.main()
