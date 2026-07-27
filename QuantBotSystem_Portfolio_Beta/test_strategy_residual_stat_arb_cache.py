import tempfile
import unittest
from pathlib import Path
import sqlite3
from unittest.mock import patch

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

import strategy_residual_stat_arb as subject
from strategy_residual_stat_arb_cache import (
    ResidualCacheContext,
    ResidualStatArbCache,
)


def _ohlcv(returns, start="2024-01-02"):
    close = 100.0 * np.cumprod(1.0 + np.asarray(returns, dtype=float))
    index = pd.date_range(start, periods=len(close), freq="B")
    return pd.DataFrame(
        {
            "Open": close * 0.999,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(len(close), 100_000.0),
        },
        index=index,
    )


def _inputs():
    axis = np.arange(48, dtype=float)
    target_return = 0.001 + 0.002 * np.sin(axis / 3.0)
    peer_one_return = target_return + 0.0002 * np.cos(axis / 4.0)
    peer_two_return = 0.5 * target_return + 0.001 * np.cos(axis / 2.0)
    market_return = 0.0006 + 0.0003 * np.sin(axis / 5.0)
    prices = {
        "TARGET": _ohlcv(target_return),
        "PEER1": _ohlcv(peer_one_return),
        "PEER2": _ohlcv(peer_two_return),
        "MARKET": _ohlcv(market_return),
    }
    metadata = {
        ticker: {
            "listing_country": "US",
            "sector": "Technology",
            "industry": "Software",
        }
        for ticker in prices
    }
    config = subject.ResidualStatArbConfig(
        train_window=12,
        min_train_observations=8,
        residual_horizon=2,
        z_window=6,
        max_peers=2,
        peer_correlation_lookback=12,
        peer_min_observations=8,
    )
    return prices, metadata, config


class ResidualCacheTests(unittest.TestCase):
    def test_persistent_cache_prunes_oldest_writes_and_keeps_newest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "residual.sqlite"
            cache = ResidualStatArbCache(
                path,
                max_entries=2,
                prune_every=1,
            )
            self.assertTrue(cache.put("features", "old", {"value": 1}))
            self.assertTrue(cache.put("features", "middle", {"value": 2}))
            self.assertTrue(cache.put("features", "old", {"value": 3}))
            self.assertTrue(cache.put("features", "newest", {"value": 4}))

            self.assertEqual(cache.get("features", "old"), {"value": 3})
            self.assertEqual(
                cache.get("features", "newest"), {"value": 4}
            )
            self.assertIsNone(cache.get("features", "middle"))

            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM residual_cache_entries"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 2)

    def test_corrupt_sqlite_payload_is_a_safe_cache_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "residual.sqlite"
            cache = ResidualStatArbCache(path)
            self.assertTrue(cache.put("peer_ranking", "key", [{"ticker": "A"}]))
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE residual_cache_entries
                    SET payload = ?
                    WHERE namespace = ? AND cache_key = ?
                    """,
                    (sqlite3.Binary(b"not-a-pickle"), "peer_ranking", "key"),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertIsNone(cache.get("peer_ranking", "key"))

    def test_peer_ranking_reuses_memory_then_sqlite(self):
        prices, metadata, config = _inputs()
        cutoff = prices["TARGET"].index[-1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "residual.sqlite"
            first_context = subject.create_residual_cache_context(
                prices,
                metadata,
                cache_path=path,
            )
            first = subject.rank_peer_candidates(
                "TARGET",
                prices,
                metadata,
                as_of=cutoff,
                config=config,
                cache_context=first_context,
            )
            second = subject.rank_peer_candidates(
                "TARGET",
                prices,
                metadata,
                as_of=cutoff,
                config=config,
                cache_context=first_context,
            )
            self.assertEqual(first, second)
            self.assertEqual(first_context.stats["misses"], 1)
            self.assertEqual(first_context.stats["memory_hits"], 1)

            second_context = subject.create_residual_cache_context(
                prices,
                metadata,
                cache_path=path,
            )
            third = subject.rank_peer_candidates(
                "TARGET",
                prices,
                metadata,
                as_of=cutoff,
                config=config,
                cache_context=second_context,
            )
            self.assertEqual(first, third)
            self.assertEqual(second_context.stats["disk_hits"], 1)

    def test_keys_invalidate_on_cutoff_config_metadata_universe_and_close(self):
        prices, metadata, config = _inputs()
        normalized = subject.normalize_price_map(prices)
        cutoff = normalized["TARGET"].index[-1]
        base_context = ResidualCacheContext(
            normalized, metadata, enable_disk=False
        )
        base = base_context.peer_ranking_key("TARGET", cutoff, config)

        earlier = base_context.peer_ranking_key(
            "TARGET", cutoff - pd.offsets.BDay(1), config
        )
        changed_config = subject.ResidualStatArbConfig(
            **{
                **config.__dict__,
                "peer_correlation_lookback": 13,
            }
        )
        config_key = base_context.peer_ranking_key(
            "TARGET", cutoff, changed_config
        )

        changed_metadata = {
            ticker: dict(row) for ticker, row in metadata.items()
        }
        changed_metadata["PEER2"]["sector"] = "Industrials"
        metadata_context = ResidualCacheContext(
            normalized, changed_metadata, enable_disk=False
        )
        metadata_key = metadata_context.peer_ranking_key(
            "TARGET", cutoff, config
        )

        expanded_prices = dict(normalized)
        expanded_prices["EXTRA"] = _ohlcv(np.full(48, 0.0005))
        expanded_metadata = dict(metadata)
        expanded_metadata["EXTRA"] = dict(metadata["TARGET"])
        universe_context = ResidualCacheContext(
            expanded_prices, expanded_metadata, enable_disk=False
        )
        universe_key = universe_context.peer_ranking_key(
            "TARGET", cutoff, config
        )

        changed_prices = {
            ticker: frame.copy() for ticker, frame in normalized.items()
        }
        changed_prices["PEER1"].iloc[
            -3, changed_prices["PEER1"].columns.get_loc("Close")
        ] *= 1.01
        price_context = ResidualCacheContext(
            changed_prices, metadata, enable_disk=False
        )
        close_key = price_context.peer_ranking_key(
            "TARGET", cutoff, config
        )

        self.assertEqual(
            len({base, earlier, config_key, metadata_key, universe_key, close_key}),
            6,
        )

    def test_residual_features_are_copied_and_preserve_lagged_regression(self):
        prices, metadata, config = _inputs()
        context = subject.create_residual_cache_context(
            prices,
            metadata,
            enable_disk=False,
        )
        peers = ["PEER1", "PEER2"]
        first = subject.build_residual_features(
            "TARGET",
            prices,
            metadata,
            peers=peers,
            as_of=prices["TARGET"].index[-1],
            config=config,
            cache_context=context,
        )
        expected = first.copy(deep=True)
        first.loc[first.index[-1], "HEDGE_BETA"] = 999.0
        second = subject.build_residual_features(
            "TARGET",
            prices,
            metadata,
            peers=peers,
            as_of=prices["TARGET"].index[-1],
            config=config,
            cache_context=context,
        )

        assert_frame_equal(second, expected)
        trained = second["BETA_TRAIN_END"].notna()
        self.assertTrue(trained.any())
        self.assertTrue(
            (
                pd.DatetimeIndex(second.loc[trained, "BETA_TRAIN_END"])
                < second.index[trained]
            ).all()
        )
        self.assertEqual(context.stats["memory_hits"], 1)

    def test_cache_failures_fall_back_to_normal_calculation(self):
        prices, metadata, config = _inputs()
        cutoff = prices["TARGET"].index[-1]
        expected = subject.rank_peer_candidates(
            "TARGET",
            prices,
            metadata,
            as_of=cutoff,
            config=config,
        )

        class BrokenCache:
            def peer_ranking_key(self, *args):
                raise RuntimeError("key failure")

            def get(self, *args):
                raise RuntimeError("read failure")

            def put(self, *args):
                raise RuntimeError("write failure")

        actual = subject.rank_peer_candidates(
            "TARGET",
            prices,
            metadata,
            as_of=cutoff,
            config=config,
            cache_context=BrokenCache(),
        )
        self.assertEqual(actual, expected)

    def test_scan_progress_callback_runs_once_per_ticker_and_is_isolated(self):
        statuses = {
            "A": "NO_SIGNAL",
            "B": "DATA_INSUFFICIENT",
            "C": "OK",
        }
        calls = []

        def analyze(ticker, *args, **kwargs):
            row = {
                "ticker": ticker,
                "status": statuses[ticker],
            }
            if statuses[ticker] == "OK":
                row.update({"side": "LONG", "signal_score": 1.0})
            return row

        def callback(completed, total, ticker, succeeded):
            calls.append((completed, total, ticker, succeeded))
            raise RuntimeError("Telegram edit failed")

        with patch.object(
            subject, "analyze_residual_stat_arb", side_effect=analyze
        ):
            result = subject.scan_residual_stat_arb(
                ["A", "B", "C"],
                {},
                {},
                progress_callback=callback,
            )

        self.assertEqual(
            calls,
            [
                (1, 3, "A", True),
                (2, 3, "B", False),
                (3, 3, "C", True),
            ],
        )
        self.assertEqual([row["ticker"] for row in result["signals"]], ["C"])

    def test_scan_progress_reports_failed_analysis_attempt_once(self):
        calls = []
        with patch.object(
            subject,
            "analyze_residual_stat_arb",
            side_effect=RuntimeError("analysis failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "analysis failed"):
                subject.scan_residual_stat_arb(
                    ["A"],
                    {},
                    {},
                    progress_callback=lambda *args: calls.append(args),
                )
        self.assertEqual(calls, [(1, 1, "A", False)])


if __name__ == "__main__":
    unittest.main()
