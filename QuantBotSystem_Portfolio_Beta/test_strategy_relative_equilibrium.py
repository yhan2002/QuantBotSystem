import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

import strategy_relative_equilibrium as strategy


def prices(dates, opens=None):
    opens = opens or [100.0 + index for index in range(len(dates))]
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [value + 1.0 for value in opens],
            "Low": [value - 1.0 for value in opens],
            "Close": [value + 0.5 for value in opens],
            "Adj Close": [value + 0.5 for value in opens],
            "Volume": [1_000_000.0] * len(dates),
        },
        index=dates,
    )


class RelativeEquilibriumTests(unittest.TestCase):
    def test_apac_market_factor_uses_only_prior_us_close(self):
        dates = pd.date_range("2026-01-05", periods=3, freq="D")
        market = pd.Series([0.01, 0.02, 0.03], index=dates)

        us = strategy._lagged_market_returns(market, dates, "US")
        apac = strategy._lagged_market_returns(market, dates, "KR")

        self.assertEqual(0.02, us.iloc[1])
        self.assertEqual(0.01, apac.iloc[1])
        self.assertTrue(pd.isna(apac.iloc[0]))

    def test_rolling_model_does_not_rewrite_past_when_future_changes(self):
        rng = np.random.default_rng(7)
        dates = pd.bdate_range("2025-01-02", periods=150)
        market = rng.normal(0.0, 0.01, len(dates))
        sector = rng.normal(0.0, 0.008, len(dates))
        level = np.zeros(len(dates))
        for index in range(1, len(level)):
            level[index] = 0.90 * level[index - 1] + rng.normal(0.0, 0.004)
        residual = np.diff(level, prepend=0.0)
        factors = pd.DataFrame(
            {
                "TARGET_RETURN": 0.8 * market + 0.6 * sector + residual,
                "MARKET_RETURN": market,
                "SECTOR_EXCESS_RETURN": sector,
                "SECTOR_RETURN": market + sector,
                "TURNOVER_RATIO": 1.0,
            },
            index=dates,
        )
        config = strategy.RelativeEquilibriumConfig(
            regression_window=40,
            min_observations=30,
            min_half_life=1.0,
            max_half_life=100.0,
        )
        baseline = strategy._rolling_models(factors, config)
        changed = factors.copy()
        changed.loc[dates[121]:, "TARGET_RETURN"] += 1.0
        rerun = strategy._rolling_models(changed, config)

        assert_frame_equal(baseline.loc[:dates[120]], rerun.loc[:dates[120]])
        self.assertTrue(bool(baseline.loc[dates[120], "MODEL_READY"]))

    def test_turnover_veto_blocks_an_otherwise_valid_long(self):
        date = pd.Timestamp("2026-01-05")
        features = pd.DataFrame(
            {
                "MODEL_READY": [True],
                "MODEL_VALID": [True],
                "S_SCORE": [-2.0],
                "HALF_LIFE": [8.0],
                "TURNOVER_RATIO": [2.0],
                "HEDGE_GROSS": [1.0],
                "RESIDUAL_VOL": [0.01],
                "BETA_MARKET": [1.0],
                "BETA_SECTOR": [0.5],
                "HEDGE_BETA": [0.8],
            },
            index=[date],
        )
        with patch.object(
            strategy,
            "build_relative_equilibrium_features",
            return_value=(features, ["P1", "P2", "P3"]),
        ):
            result = strategy.analyze_relative_equilibrium(
                "AAA", {}, {}, config=strategy.DEFAULT_CONFIG
            )

        self.assertEqual("NO_SIGNAL", result["status"])
        self.assertEqual("turnover veto", result["reason"])

    def test_close_signal_enters_and_exits_at_following_opens(self):
        dates = pd.bdate_range("2026-01-01", periods=7)
        price_map = {
            ticker: prices(dates, [100 + offset + index for index in range(7)])
            for offset, ticker in enumerate(
                ["AAA", strategy.MARKET_TICKER, "P1", "P2", "P3"]
            )
        }
        features = pd.DataFrame(
            {
                "TARGET_RETURN": [0.0, -0.02, 0.0, 2.0, 0.0, 0.0, 0.0],
                "MARKET_RETURN": [0.0] * 7,
                "SECTOR_EXCESS_RETURN": [0.0] * 7,
                "TURNOVER_RATIO": [1.0] * 7,
                "MODEL_VALID": [False, True, True, True, True, True, True],
                "S_SCORE": [0.0, -2.0, -2.0, 0.0, 0.0, 0.0, 0.0],
                "RESIDUAL_VOL": [0.01] * 7,
                "BETA_MARKET": [1.0] * 7,
                "BETA_SECTOR": [0.5] * 7,
                "HEDGE_BETA": [0.9] * 7,
                "HALF_LIFE": [5.0] * 7,
                "SPREAD_LEVEL": [-2.0] * 7,
                "ALPHA": [0.0] * 7,
                "OU_MEAN": [0.0] * 7,
                "OU_SIGMA": [1.0] * 7,
            },
            index=dates,
        )
        with patch.object(
            strategy,
            "build_relative_equilibrium_features",
            return_value=(features, ["P1", "P2", "P3"]),
        ):
            result = strategy.run_relative_equilibrium_backtest(
                "AAA",
                include_trades=True,
                price_map=price_map,
                metadata_by_ticker={},
                evaluation_start=dates[1],
                evaluation_end=dates[-1],
            )
            short_only = strategy.run_relative_equilibrium_backtest(
                "AAA",
                side="SHORT",
                include_trades=True,
                price_map=price_map,
                metadata_by_ticker={},
                evaluation_start=dates[1],
                evaluation_end=dates[-1],
            )
            with self.assertRaisesRegex(ValueError, "NEXT_OPEN"):
                strategy.run_relative_equilibrium_backtest(
                    "AAA",
                    price_map=price_map,
                    metadata_by_ticker={},
                    evaluation_start=dates[1],
                    evaluation_end=dates[-1],
                    entry_execution="NEXT_DAY_OHLC4",
                )

        self.assertEqual(1, result[2])
        trade = result[4][0]
        self.assertEqual(dates[2].date().isoformat(), trade["entry_date"])
        self.assertEqual(dates[4].date().isoformat(), trade["exit_events"][0]["date"])
        self.assertEqual("NEXT_OPEN", trade["exit_events"][0]["execution"])
        self.assertEqual(4, len(trade["legs"]))
        self.assertNotIn(strategy.MARKET_TICKER, {leg["ticker"] for leg in trade["legs"]})
        self.assertEqual(0, short_only[2])
        self.assertNotEqual("N/A", short_only[3])


if __name__ == "__main__":
    unittest.main()
