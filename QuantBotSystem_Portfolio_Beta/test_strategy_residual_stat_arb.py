import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import strategy_residual_stat_arb as subject


def _ohlcv_from_returns(returns, start="2025-01-02"):
    returns = np.asarray(returns, dtype=float)
    close = 100.0 * np.cumprod(np.r_[1.0, 1.0 + returns])
    dates = pd.date_range(start, periods=len(close), freq="B")
    previous = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "Open": previous,
            "High": np.maximum(previous, close) + 0.5,
            "Low": np.minimum(previous, close) - 0.5,
            "Close": close,
            "Volume": 1000.0,
        },
        index=dates,
    )


def _metadata():
    return {
        "TARGET": {"listing_country": "US", "sector": "Technology"},
        "PEER1": {"listing_country": "US", "sector": "Technology"},
        "PEER2": {"listing_country": "US", "sector": "Technology"},
        "MARKET": {"listing_country": "US", "sector": "Industrials"},
    }


def _small_config(**overrides):
    values = {
        "train_window": 12,
        "min_train_observations": 8,
        "residual_horizon": 2,
        "z_window": 5,
        "entry_extreme_z": 2.0,
        "residual_stop_z": 3.5,
        "max_holding_days": 3,
        "max_peers": 2,
        "peer_correlation_lookback": 12,
        "peer_min_observations": 8,
        "max_hedge_beta": 3.0,
    }
    values.update(overrides)
    return subject.ResidualStatArbConfig(**values)


def _patched_backtest_features(index, signal_index, hedge_beta=0.5):
    features = pd.DataFrame(
        {
            "LONG_SIGNAL": False,
            "SHORT_SIGNAL": False,
            "SIGNAL_SCORE": 0.0,
            "RESIDUAL_Z": 1.0,
            "PREVIOUS_RESIDUAL_Z": -1.0,
            "RESIDUAL_Z_MEAN": 0.0,
            "RESIDUAL_Z_STD": 0.01,
            "TARGET_RETURN": 0.0,
            "PEER_RETURN": 0.02,
            "HEDGE_ALPHA": 0.0,
            "HEDGE_BETA": hedge_beta,
            "BETA_MARKET": 0.6,
            "BETA_SECTOR_EXCESS": 0.4,
        },
        index=index,
    )
    features.iloc[signal_index, features.columns.get_loc("LONG_SIGNAL")] = True
    features.iloc[signal_index, features.columns.get_loc("SIGNAL_SCORE")] = 2.7
    features.iloc[signal_index, features.columns.get_loc("RESIDUAL_Z")] = -1.7
    features.iloc[
        signal_index, features.columns.get_loc("PREVIOUS_RESIDUAL_Z")
    ] = -2.7
    return features


class ResidualStatArbTests(unittest.TestCase):
    def test_beta_estimation_excludes_current_row(self):
        count = 35
        axis = np.arange(count, dtype=float)
        market_returns = 0.001 + 0.0015 * np.sin(axis / 3.0)
        peer_returns = market_returns + 0.001 * np.cos(axis / 4.0)
        target_returns = (
            0.35 * market_returns
            + 0.9 * peer_returns
            + 0.0003 * np.sin(axis * 1.7)
        )
        price_map = {
            "TARGET": _ohlcv_from_returns(target_returns),
            "PEER1": _ohlcv_from_returns(peer_returns),
            "MARKET": _ohlcv_from_returns(market_returns),
        }
        config = _small_config(max_peers=1)
        baseline = subject.build_residual_features(
            "TARGET",
            price_map,
            _metadata(),
            peers=["PEER1"],
            config=config,
        )

        changed_map = {ticker: frame.copy() for ticker, frame in price_map.items()}
        changed_map["TARGET"].iloc[-1, changed_map["TARGET"].columns.get_loc("Close")] *= 1.40
        changed_map["PEER1"].iloc[-1, changed_map["PEER1"].columns.get_loc("Close")] *= 0.70
        changed = subject.build_residual_features(
            "TARGET",
            changed_map,
            _metadata(),
            peers=["PEER1"],
            config=config,
        )

        for column in (
            "BETA_MARKET",
            "BETA_SECTOR_EXCESS",
            "HEDGE_BETA",
        ):
            self.assertAlmostEqual(
                baseline[column].iloc[-1],
                changed[column].iloc[-1],
                places=12,
            )
        self.assertNotAlmostEqual(
            baseline["RESIDUAL"].iloc[-1],
            changed["RESIDUAL"].iloc[-1],
            places=6,
        )
        self.assertLess(
            baseline["BETA_TRAIN_END"].iloc[-1], baseline.index[-1]
        )

    def test_trade_leg_signs_and_gross_are_beta_adjusted(self):
        long_legs = subject.build_trade_legs(
            "TARGET", ["PEER1", "PEER2"], "LONG", hedge_beta=0.8
        )
        short_legs = subject.build_trade_legs(
            "TARGET", ["PEER1", "PEER2"], "SHORT", hedge_beta=0.8
        )

        self.assertEqual(
            [leg["signed_ratio"] for leg in long_legs],
            [1.0, -0.4, -0.4],
        )
        self.assertEqual(
            [leg["signed_ratio"] for leg in short_legs],
            [-1.0, 0.4, 0.4],
        )
        self.assertAlmostEqual(
            subject.summarize_leg_ratios(long_legs)["gross_ratio"], 1.8
        )
        self.assertAlmostEqual(
            subject.summarize_leg_ratios(long_legs)["net_ratio"], 0.2
        )

    def test_peer_ranking_respects_explicit_historical_cutoff(self):
        count = 36
        base = np.where(np.arange(count) % 2 == 0, 0.012, -0.009)
        switch = 18
        peer1_returns = np.r_[base[:switch], -base[switch:]]
        peer2_returns = np.r_[-base[:switch], base[switch:]]
        price_map = {
            "TARGET": _ohlcv_from_returns(base),
            "PEER1": _ohlcv_from_returns(peer1_returns),
            "PEER2": _ohlcv_from_returns(peer2_returns),
            "MARKET": _ohlcv_from_returns(0.2 * base),
        }
        config = _small_config(
            max_peers=1,
            peer_correlation_lookback=10,
            peer_min_observations=8,
        )
        cutoff = price_map["TARGET"].index[switch]

        historical = subject.rank_peer_candidates(
            "TARGET",
            price_map,
            _metadata(),
            as_of=cutoff,
            config=config,
        )
        current = subject.rank_peer_candidates(
            "TARGET",
            price_map,
            _metadata(),
            as_of=price_map["TARGET"].index[-1],
            config=config,
        )

        self.assertEqual(historical[0]["ticker"], "PEER1")
        self.assertEqual(current[0]["ticker"], "PEER2")
        self.assertLess(
            pd.Timestamp(historical[0]["correlation_end"]), cutoff
        )
        analysis = subject.analyze_residual_stat_arb(
            "TARGET",
            price_map,
            _metadata(),
            as_of=price_map["TARGET"].index[-1],
            peer_selection_as_of=cutoff,
            config=config,
        )
        self.assertEqual(analysis["selected_peers"], ["PEER1"])
        self.assertEqual(
            analysis["peer_selection_date"], cutoff.date().isoformat()
        )

    def test_fixed_peer_cutoff_never_moves_after_signal_date(self):
        count = 20
        axis = np.arange(count, dtype=float)
        price_map = {
            "TARGET": _ohlcv_from_returns(0.001 + 0.0002 * np.sin(axis)),
            "PEER1": _ohlcv_from_returns(0.001 + 0.0002 * np.cos(axis)),
            "MARKET": _ohlcv_from_returns(0.0008 + 0.0001 * np.sin(axis)),
        }
        signal_date = price_map["TARGET"].index[10]
        later_cutoff = price_map["TARGET"].index[15]

        result = subject.analyze_residual_stat_arb(
            "TARGET",
            price_map,
            _metadata(),
            as_of=signal_date,
            peer_selection_as_of=later_cutoff,
            config=_small_config(max_peers=1),
        )

        self.assertEqual(result["status"], "DATA_INSUFFICIENT")
        self.assertIn("predates", result["reason"])

    def test_reentry_signal_is_built_from_actual_lagged_residual_path(self):
        count = 42
        axis = np.arange(count, dtype=float)
        peer = 0.002 * np.sin(axis / 2.0) + 0.001 * np.cos(axis / 5.0)
        noise = 0.0005 * np.sin(axis * 1.9)
        noise[-4] = -0.030
        noise[-3] = 0.000
        target = 0.8 * peer + noise
        factors = pd.DataFrame(
            {
                "TARGET_RETURN": target,
                "MARKET_RETURN": 0.4 * peer,
                "PEER_RETURN": peer,
                "SECTOR_EXCESS_RETURN": 0.6 * peer,
            },
            index=pd.date_range("2025-01-02", periods=count, freq="B"),
        )
        config = _small_config(
            train_window=12,
            min_train_observations=8,
            residual_horizon=1,
            z_window=6,
        )

        with patch.object(
            subject, "build_factor_returns", return_value=factors
        ):
            features = subject.build_residual_features(
                "TARGET", {}, {}, peers=["PEER1"], config=config
            )

        long_dates = features.index[features["LONG_SIGNAL"]]
        self.assertGreaterEqual(len(long_dates), 1)
        signal_date = long_dates[-1]
        self.assertLessEqual(
            features.loc[signal_date, "PREVIOUS_RESIDUAL_Z"],
            -config.entry_extreme_z,
        )
        self.assertGreater(
            features.loc[signal_date, "RESIDUAL_Z"],
            -config.entry_extreme_z,
        )
        self.assertGreater(features.loc[signal_date, "SIGNAL_SCORE"], 0.0)

    def test_capped_hedge_beta_recomputes_matching_alpha(self):
        count = 30
        axis = np.arange(count, dtype=float)
        peer_returns = 0.001 + 0.001 * np.sin(axis / 3.0)
        target_returns = 5.0 * peer_returns + 0.0001 * np.cos(axis)
        price_map = {
            "TARGET": _ohlcv_from_returns(target_returns),
            "PEER1": _ohlcv_from_returns(peer_returns),
            "MARKET": _ohlcv_from_returns(0.5 * peer_returns),
        }
        config = _small_config(max_peers=1, max_hedge_beta=1.5)
        features = subject.build_residual_features(
            "TARGET",
            price_map,
            _metadata(),
            peers=["PEER1"],
            config=config,
        )
        latest = features.iloc[-1]
        training = features.iloc[-1 - config.train_window : -1][
            ["TARGET_RETURN", "MARKET_RETURN", "SECTOR_EXCESS_RETURN", "PEER_RETURN"]
        ].dropna()
        expected_alpha = (
            training["TARGET_RETURN"].mean()
            - latest["HEDGE_BETA"] * training["PEER_RETURN"].mean()
        )

        self.assertAlmostEqual(latest["HEDGE_BETA"], 1.5)
        self.assertAlmostEqual(latest["HEDGE_ALPHA"], expected_alpha, places=12)

    def test_backtest_enters_every_leg_at_next_open(self):
        count = 18
        axis = np.arange(count, dtype=float)
        target = _ohlcv_from_returns(0.001 + 0.0002 * np.sin(axis))
        peer1 = _ohlcv_from_returns(0.0008 + 0.0002 * np.cos(axis))
        peer2 = _ohlcv_from_returns(0.0009 + 0.0001 * np.sin(axis / 2.0))
        market = _ohlcv_from_returns(0.0007 + 0.0001 * np.cos(axis / 2.0))
        price_map = {
            "TARGET": target,
            "PEER1": peer1,
            "PEER2": peer2,
            "MARKET": market,
        }
        signal_index = 7
        entry_index = signal_index + 1
        price_map["TARGET"].iloc[
            entry_index, price_map["TARGET"].columns.get_loc("Open")
        ] = 123.45
        price_map["PEER1"].iloc[
            entry_index, price_map["PEER1"].columns.get_loc("Open")
        ] = 77.0
        price_map["PEER2"].iloc[
            entry_index, price_map["PEER2"].columns.get_loc("Open")
        ] = 88.0
        signal_date = target.index[signal_index]
        config = _small_config(
            train_window=4,
            min_train_observations=2,
            residual_horizon=1,
            z_window=2,
            max_holding_days=2,
            peer_correlation_lookback=4,
            peer_min_observations=2,
        )

        features = pd.DataFrame(
            {
                "LONG_SIGNAL": False,
                "SHORT_SIGNAL": False,
                "SIGNAL_SCORE": 0.0,
                "RESIDUAL_Z": -1.0,
                "PREVIOUS_RESIDUAL_Z": -1.0,
                "RESIDUAL_Z_MEAN": 0.0,
                "RESIDUAL_Z_STD": 0.01,
                "TARGET_RETURN": 0.0,
                "PEER_RETURN": 0.01,
                "HEDGE_ALPHA": 0.0,
                "HEDGE_BETA": 0.8,
                "BETA_MARKET": 0.6,
                "BETA_SECTOR_EXCESS": 0.4,
            },
            index=target.index,
        )
        features.loc[signal_date, "LONG_SIGNAL"] = True
        features.loc[signal_date, "SIGNAL_SCORE"] = 2.7
        features.loc[signal_date, "RESIDUAL_Z"] = -1.7
        features.loc[signal_date, "PREVIOUS_RESIDUAL_Z"] = -2.7
        ranked = [
            {"ticker": "PEER1", "correlation": 0.9, "observations": 10},
            {"ticker": "PEER2", "correlation": 0.8, "observations": 10},
        ]

        with patch.object(
            subject, "rank_peer_candidates", return_value=ranked
        ), patch.object(
            subject, "build_residual_features", return_value=features
        ):
            result = subject.backtest_residual_stat_arb(
                "TARGET",
                price_map,
                _metadata(),
                side="LONG",
                evaluation_start=target.index[1],
                config=config,
            )

        self.assertEqual(result["count"], 1)
        trade = result["trades"][0]
        self.assertEqual(
            trade["entry_date"], target.index[entry_index].date().isoformat()
        )
        self.assertNotEqual(trade["entry_date"], trade["signal_date"])
        self.assertEqual(trade["entry_execution"], subject.NEXT_OPEN)
        self.assertEqual(trade["entry_price"], 123.45)
        self.assertEqual(
            [leg["entry_price"] for leg in trade["legs"]],
            [123.45, 77.0, 88.0],
        )
        self.assertEqual(
            [leg["signed_ratio"] for leg in trade["legs"]],
            [1.0, -0.4, -0.4],
        )
        self.assertAlmostEqual(trade["gross_ratio"], 1.8)
        self.assertGreater(trade["exit_date"], trade["exit_signal_date"])

    def test_exit_uses_entry_frozen_hedge_model_not_rolling_feature_z(self):
        count = 12
        axis = np.arange(count, dtype=float)
        target = _ohlcv_from_returns(0.001 + 0.0001 * np.sin(axis))
        peer = _ohlcv_from_returns(0.001 + 0.0001 * np.cos(axis))
        market = _ohlcv_from_returns(0.0008 + 0.0001 * np.sin(axis / 2.0))
        price_map = {"TARGET": target, "PEER1": peer, "MARKET": market}
        signal_index = 4
        features = _patched_backtest_features(
            target.index, signal_index, hedge_beta=0.5
        )
        config = _small_config(
            train_window=4,
            min_train_observations=2,
            residual_horizon=1,
            z_window=2,
            max_holding_days=2,
            max_peers=1,
            peer_correlation_lookback=4,
            peer_min_observations=2,
        )
        ranked = [{"ticker": "PEER1", "correlation": 0.9, "observations": 10}]

        with patch.object(
            subject, "rank_peer_candidates", return_value=ranked
        ), patch.object(
            subject, "build_residual_features", return_value=features
        ):
            result = subject.backtest_residual_stat_arb(
                "TARGET",
                price_map,
                _metadata(),
                side="LONG",
                evaluation_start=target.index[1],
                config=config,
            )

        trade = result["trades"][0]
        # Rolling feature z is +1 on the entry close, but the held model z is
        # -1, so zero exit must not fire and the two-day time exit wins.
        self.assertEqual(trade["exit_reason"], "2d time exit")
        expected_signal = target.index[signal_index + 2].date().isoformat()
        self.assertEqual(trade["exit_signal_date"], expected_signal)
        self.assertAlmostEqual(trade["held_residual_z_latest"], -1.0)

    def test_no_prices_in_evaluation_window_is_data_insufficient(self):
        target = _ohlcv_from_returns(np.full(8, 0.001))
        evaluation_start = target.index[-1] + pd.offsets.BDay(1)
        price_map = {"TARGET": target}

        result = subject.backtest_residual_stat_arb(
            "TARGET",
            price_map,
            _metadata(),
            evaluation_start=evaluation_start,
        )
        wrapped = subject.run_residual_stat_arb_backtest(
            "TARGET",
            price_map,
            _metadata(),
            include_trades=True,
            evaluation_start=evaluation_start,
        )

        self.assertEqual(result["status"], "DATA_INSUFFICIENT")
        self.assertEqual(
            result["reason"], "no target price exists in the evaluation window"
        )
        self.assertEqual(wrapped[3], "N/A")
        self.assertEqual(wrapped[4], [])

    def test_open_trade_is_marked_at_evaluation_end_close(self):
        count = 10
        axis = np.arange(count, dtype=float)
        target = _ohlcv_from_returns(0.001 + 0.0001 * np.sin(axis))
        peer = _ohlcv_from_returns(0.0009 + 0.0001 * np.cos(axis))
        market = _ohlcv_from_returns(0.0007 + 0.0001 * np.sin(axis / 2.0))
        future_peer_row = peer.iloc[[-1]].copy()
        future_peer_row.index = [target.index[-1] + pd.offsets.BDay(1)]
        future_peer_row.loc[:, ["Open", "High", "Low", "Close"]] = 999.0
        peer_with_future = pd.concat([peer, future_peer_row])
        price_map = {
            "TARGET": target,
            "PEER1": peer_with_future,
            "MARKET": market,
        }
        signal_index = len(target) - 2
        features = _patched_backtest_features(
            target.index, signal_index, hedge_beta=0.5
        )
        config = _small_config(
            train_window=4,
            min_train_observations=2,
            residual_horizon=1,
            z_window=2,
            max_holding_days=10,
            max_peers=1,
            peer_correlation_lookback=4,
            peer_min_observations=2,
        )
        ranked = [{"ticker": "PEER1", "correlation": 0.9, "observations": 10}]

        with patch.object(
            subject, "rank_peer_candidates", return_value=ranked
        ), patch.object(
            subject, "build_residual_features", return_value=features
        ):
            result = subject.backtest_residual_stat_arb(
                "TARGET",
                price_map,
                _metadata(),
                side="LONG",
                evaluation_start=target.index[1],
                config=config,
            )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["evaluation_end_marks"], 1)
        self.assertIsNone(result["open_trade"])
        trade = result["trades"][0]
        self.assertEqual(
            trade["exit_execution_status"], "MARKED_AT_EVALUATION_END"
        )
        self.assertEqual(trade["exit_date"], target.index[-1].date().isoformat())
        self.assertEqual(
            [leg["exit_price"] for leg in trade["legs"]],
            [target["Close"].iloc[-1], peer["Close"].iloc[-1]],
        )

    def test_missing_peer_next_open_is_recorded_before_delayed_exit(self):
        count = 12
        axis = np.arange(count, dtype=float)
        target = _ohlcv_from_returns(0.001 + 0.0001 * np.sin(axis))
        peer = _ohlcv_from_returns(0.001 + 0.0001 * np.cos(axis))
        market = _ohlcv_from_returns(0.0008 + 0.0001 * np.sin(axis / 2.0))
        signal_index = 3
        intended_exit_index = signal_index + 2
        peer.iloc[
            intended_exit_index, peer.columns.get_loc("Open")
        ] = np.nan
        price_map = {"TARGET": target, "PEER1": peer, "MARKET": market}
        features = _patched_backtest_features(
            target.index, signal_index, hedge_beta=0.5
        )
        config = _small_config(
            train_window=4,
            min_train_observations=2,
            residual_horizon=1,
            z_window=2,
            max_holding_days=1,
            max_peers=1,
            peer_correlation_lookback=4,
            peer_min_observations=2,
        )
        ranked = [{"ticker": "PEER1", "correlation": 0.9, "observations": 10}]

        with patch.object(
            subject, "rank_peer_candidates", return_value=ranked
        ), patch.object(
            subject, "build_residual_features", return_value=features
        ):
            result = subject.backtest_residual_stat_arb(
                "TARGET",
                price_map,
                _metadata(),
                side="LONG",
                evaluation_start=target.index[1],
                config=config,
            )

        trade = result["trades"][0]
        self.assertEqual(
            trade["exit_execution_status"],
            "FILLED_DELAYED_AFTER_MISSING_OPEN",
        )
        self.assertEqual(len(trade["exit_execution_failures"]), 1)
        failure = trade["exit_execution_failures"][0]
        self.assertEqual(failure["missing_legs"], ["PEER1"])
        self.assertEqual(
            failure["intended_exit_date"],
            target.index[intended_exit_index].date().isoformat(),
        )
        self.assertEqual(
            trade["exit_date"],
            target.index[intended_exit_index + 1].date().isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
