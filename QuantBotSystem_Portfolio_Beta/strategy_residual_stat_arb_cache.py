"""Failure-tolerant cache support for the residual statistical-arbitrage engine.

The shared market database is deliberately never touched here.  Cache records
live in a Portfolio Beta-only SQLite file and are content-addressed from the
immutable input snapshot used by one scan/backtest job.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import pickle
from pathlib import Path
import sqlite3
import threading
from typing import Any

import numpy as np
import pandas as pd

try:
    import runtime_settings
except ImportError:  # pragma: no cover - package-style import fallback
    from . import runtime_settings


CACHE_SCHEMA_VERSION = 1
ENGINE_VERSION = "residual-stat-arb-2026.07.27.1"
DEFAULT_MAX_ENTRIES = 4_000
DEFAULT_PRUNE_EVERY = 100
DEFAULT_CACHE_PATH = (
    Path(runtime_settings.DATA_DIR)
    / "cache"
    / "residual_stat_arb_cache.sqlite"
)
_CACHE_TABLE = "residual_cache_entries"


def _timestamp(value):
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp


def _canonical_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if np.isnan(value):
            return "NaN"
        if np.isposinf(value):
            return "Infinity"
        if np.isneginf(value):
            return "-Infinity"
        return value
    if isinstance(value, np.generic):
        return _canonical_value(value.item())
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if is_dataclass(value):
        return _canonical_value(asdict(value))
    if hasattr(value, "items"):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])
            )
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_canonical_value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item, sort_keys=True, ensure_ascii=True
                ),
            )
        return items
    return str(value)


def _stable_digest(value):
    encoded = json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _close_series(prices):
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        return pd.Series(dtype=float)
    if "Close" not in prices.columns:
        return pd.Series(dtype=float)
    values = pd.to_numeric(prices["Close"], errors="coerce")
    index = pd.to_datetime(values.index, errors="coerce")
    valid = ~pd.isna(index)
    values = values.loc[valid].copy()
    index = pd.DatetimeIndex(index[valid])
    if index.tz is not None:
        index = index.tz_convert(None)
    values.index = index
    return values[~values.index.duplicated(keep="last")].sort_index()


class ResidualStatArbCache:
    """Small atomic SQLite blob store.

    Every operation opens its own connection so the object can safely be used
    by the background worker without sharing a SQLite connection across
    threads.  Any filesystem, SQLite, or deserialization error is converted to
    a cache miss by :class:`ResidualCacheContext`.
    """

    def __init__(
        self,
        path=None,
        *,
        max_entries=DEFAULT_MAX_ENTRIES,
        prune_every=DEFAULT_PRUNE_EVERY,
    ):
        self.path = Path(path or DEFAULT_CACHE_PATH)
        self.max_entries = int(max_entries)
        self.prune_every = int(prune_every)
        if self.max_entries < 1:
            raise ValueError("max_entries must be positive")
        if self.prune_every < 1:
            raise ValueError("prune_every must be positive")
        self._write_count = 0
        self._write_lock = threading.Lock()

    def _should_prune(self):
        with self._write_lock:
            self._write_count += 1
            return self._write_count % self.prune_every == 0

    def _prune(self, connection):
        connection.execute(
            f"""
            DELETE FROM {_CACHE_TABLE}
            WHERE rowid IN (
                SELECT rowid
                FROM {_CACHE_TABLE}
                ORDER BY created_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_entries,),
        )

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=3.0)
        connection.execute("PRAGMA busy_timeout = 3000")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                engine_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            )
            """
        )
        return connection

    def get(self, namespace, cache_key):
        connection = None
        try:
            connection = self._connect()
            row = connection.execute(
                f"""
                SELECT schema_version, engine_version, payload
                FROM {_CACHE_TABLE}
                WHERE namespace = ? AND cache_key = ?
                """,
                (str(namespace), str(cache_key)),
            ).fetchone()
            if row is None:
                return None
            schema_version, engine_version, payload = row
            if (
                int(schema_version) != CACHE_SCHEMA_VERSION
                or str(engine_version) != ENGINE_VERSION
            ):
                return None
            return pickle.loads(payload)
        except Exception:
            return None
        finally:
            if connection is not None:
                connection.close()

    def put(self, namespace, cache_key, value):
        connection = None
        try:
            payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            connection = self._connect()
            connection.execute(
                f"""
                INSERT INTO {_CACHE_TABLE} (
                    namespace,
                    cache_key,
                    schema_version,
                    engine_version,
                    created_at,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, cache_key) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    engine_version = excluded.engine_version,
                    created_at = excluded.created_at,
                    payload = excluded.payload
                """,
                (
                    str(namespace),
                    str(cache_key),
                    CACHE_SCHEMA_VERSION,
                    ENGINE_VERSION,
                    datetime.now(timezone.utc).isoformat(
                        timespec="microseconds"
                    ),
                    sqlite3.Binary(payload),
                ),
            )
            if self._should_prune():
                self._prune(connection)
            connection.commit()
            return True
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return False
        finally:
            if connection is not None:
                connection.close()


class ResidualCacheContext:
    """Job-scoped memory cache with an optional persistent SQLite layer.

    The supplied price map and metadata are treated as an immutable job
    snapshot.  Create a fresh context after refreshing prices or metadata.
    """

    def __init__(
        self,
        price_map,
        metadata_by_ticker,
        *,
        cache=None,
        cache_path=None,
        enable_disk=True,
        max_entries=DEFAULT_MAX_ENTRIES,
        prune_every=DEFAULT_PRUNE_EVERY,
    ):
        self.price_map = price_map if hasattr(price_map, "items") else {}
        self.metadata_by_ticker = (
            metadata_by_ticker
            if hasattr(metadata_by_ticker, "get")
            else {}
        )
        self.disk_cache = cache
        if self.disk_cache is None and enable_disk:
            self.disk_cache = ResidualStatArbCache(
                cache_path,
                max_entries=max_entries,
                prune_every=prune_every,
            )
        self._memory = {}
        self._close_fingerprints = {}
        self._lock = threading.RLock()
        self._stats = {
            "memory_hits": 0,
            "disk_hits": 0,
            "misses": 0,
            "memory_writes": 0,
            "disk_writes": 0,
            "errors": 0,
        }
        self._universe = tuple(sorted(str(ticker) for ticker in self.price_map))
        metadata_snapshot = {
            ticker: _canonical_value(
                self.metadata_by_ticker.get(ticker, {})
            )
            for ticker in self._universe
        }
        peer_groups = {}
        country_groups = {}
        for ticker in self._universe:
            row = self.metadata_by_ticker.get(ticker, {})
            if not hasattr(row, "get"):
                continue
            country = (
                str(row.get("listing_country") or "").strip().casefold()
            )
            sector = str(row.get("sector") or "").strip().casefold()
            if country:
                country_groups.setdefault(country, []).append(ticker)
            if country and sector:
                peer_groups.setdefault((country, sector), []).append(ticker)
        self._country_groups = {
            key: tuple(values) for key, values in country_groups.items()
        }
        self._peer_groups = {
            key: tuple(values) for key, values in peer_groups.items()
        }
        self._universe_metadata_fingerprint = _stable_digest(
            {
                "universe": self._universe,
                "metadata": metadata_snapshot,
            }
        )

    @property
    def stats(self):
        with self._lock:
            stats = dict(self._stats)
        stats["hits"] = stats["memory_hits"] + stats["disk_hits"]
        stats["requests"] = stats["hits"] + stats["misses"]
        stats["hit_rate_pct"] = (
            100.0 * stats["hits"] / stats["requests"]
            if stats["requests"]
            else 0.0
        )
        return stats

    def _serialize(self, value):
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    def _deserialize(self, payload):
        return pickle.loads(payload)

    def get(self, namespace, cache_key):
        memory_key = (str(namespace), str(cache_key))
        with self._lock:
            payload = self._memory.get(memory_key)
            if payload is not None:
                try:
                    value = self._deserialize(payload)
                except Exception:
                    self._memory.pop(memory_key, None)
                    self._stats["errors"] += 1
                else:
                    self._stats["memory_hits"] += 1
                    return value

        if self.disk_cache is not None:
            try:
                value = self.disk_cache.get(*memory_key)
            except Exception:
                value = None
                with self._lock:
                    self._stats["errors"] += 1
            if value is not None:
                try:
                    payload = self._serialize(value)
                except Exception:
                    with self._lock:
                        self._stats["errors"] += 1
                else:
                    with self._lock:
                        self._memory[memory_key] = payload
                        self._stats["disk_hits"] += 1
                    return self._deserialize(payload)

        with self._lock:
            self._stats["misses"] += 1
        return None

    def put(self, namespace, cache_key, value):
        memory_key = (str(namespace), str(cache_key))
        try:
            payload = self._serialize(value)
        except Exception:
            with self._lock:
                self._stats["errors"] += 1
            return False

        with self._lock:
            self._memory[memory_key] = payload
            self._stats["memory_writes"] += 1

        if self.disk_cache is None:
            return True
        try:
            stored = bool(self.disk_cache.put(*memory_key, value))
        except Exception:
            stored = False
        with self._lock:
            if stored:
                self._stats["disk_writes"] += 1
            else:
                self._stats["errors"] += 1
        return True

    def _ticker_close_fingerprint(
        self,
        ticker,
        cutoff,
        *,
        include_cutoff,
    ):
        timestamp = _timestamp(cutoff)
        cache_key = (
            str(ticker),
            timestamp.isoformat() if timestamp is not None else None,
            bool(include_cutoff),
        )
        with self._lock:
            cached = self._close_fingerprints.get(cache_key)
        if cached is not None:
            return cached

        close = _close_series(self.price_map.get(ticker))
        if timestamp is not None:
            close = close[
                close.index <= timestamp
                if include_cutoff
                else close.index < timestamp
            ]
        index_values = np.asarray(close.index.asi8, dtype="<i8")
        close_values = np.asarray(close.to_numpy(dtype=float), dtype="<f8")
        digest = hashlib.sha256()
        digest.update(str(ticker).encode("utf-8"))
        digest.update(str(len(close)).encode("ascii"))
        digest.update(index_values.tobytes())
        digest.update(close_values.tobytes())
        fingerprint = digest.hexdigest()
        with self._lock:
            self._close_fingerprints[cache_key] = fingerprint
        return fingerprint

    def _content_fingerprint(
        self,
        tickers,
        cutoff,
        *,
        include_cutoff,
    ):
        rows = [
            (
                ticker,
                self._ticker_close_fingerprint(
                    ticker,
                    cutoff,
                    include_cutoff=include_cutoff,
                ),
            )
            for ticker in sorted(set(str(value) for value in tickers))
        ]
        return _stable_digest(rows)

    def _same_group_tickers(self, ticker):
        target = self.metadata_by_ticker.get(ticker, {})
        if not hasattr(target, "get"):
            return (ticker,)
        country = str(target.get("listing_country") or "").strip().casefold()
        sector = str(target.get("sector") or "").strip().casefold()
        if not country or not sector:
            return (ticker,)
        return self._peer_groups.get((country, sector), (ticker,))

    def _same_country_tickers(self, ticker):
        target = self.metadata_by_ticker.get(ticker, {})
        if not hasattr(target, "get"):
            return (ticker,)
        country = str(target.get("listing_country") or "").strip().casefold()
        if not country:
            return (ticker,)
        return self._country_groups.get(country, (ticker,))

    def _key(
        self,
        namespace,
        ticker,
        cutoff,
        config,
        *,
        relevant_tickers,
        include_cutoff,
        extra=None,
    ):
        timestamp = _timestamp(cutoff)
        return _stable_digest(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "engine_version": ENGINE_VERSION,
                "namespace": namespace,
                "target": str(ticker),
                "cutoff": (
                    timestamp.isoformat() if timestamp is not None else None
                ),
                "config": _canonical_value(config),
                "universe_metadata": self._universe_metadata_fingerprint,
                "close_content": self._content_fingerprint(
                    relevant_tickers,
                    timestamp,
                    include_cutoff=include_cutoff,
                ),
                "extra": _canonical_value(extra or {}),
            }
        )

    def peer_ranking_key(self, ticker, cutoff, config):
        return self._key(
            "peer_ranking",
            ticker,
            cutoff,
            config,
            relevant_tickers=self._same_group_tickers(ticker),
            include_cutoff=False,
        )

    def residual_features_key(self, ticker, as_of, config, peers):
        peers = tuple(str(peer) for peer in peers)
        relevant = set(self._same_country_tickers(ticker))
        relevant.add(str(ticker))
        relevant.update(peers)
        return self._key(
            "residual_features",
            ticker,
            as_of,
            config,
            relevant_tickers=relevant,
            include_cutoff=True,
            extra={"peers": peers},
        )
