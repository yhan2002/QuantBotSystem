"""Point-in-time catalyst factor for the QuantBot long/short portfolio.

This module intentionally does not use the legacy fixed catalyst score.  An
event is usable at its close only, enters no earlier than the next open, and
an author's own outcome is unavailable until its 20-trading-day exit closes.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd


HOLDING_DAYS = 20
MIN_AUTHOR_OBSERVATIONS = 10
SHRINKAGE_OBSERVATIONS = 20
AUTHOR_SCORE_MIN = -10.0
AUTHOR_SCORE_MAX = 10.0


def _as_date(value: Any) -> date:
    return pd.Timestamp(value).date()


@dataclass(frozen=True)
class CompletedOutcome:
    author: str
    exit_date: date
    directional_return_pct: float


class PointInTimeCatalystOverlay:
    """Builds 20-day catalyst tiers without looking past the as-of date."""

    def __init__(self, records: list[dict[str, Any]], price_map: dict[str, pd.DataFrame]):
        self.price_map = price_map
        self.records = []
        self.outcomes: list[CompletedOutcome] = []
        for raw in records:
            prepared = self._prepare_record(raw)
            if prepared is not None:
                self.records.append(prepared)
                outcome = self._completed_outcome(prepared)
                if outcome is not None:
                    self.outcomes.append(outcome)
        self.records.sort(key=lambda row: (row["event_date"], row["ticker"], row["author"]))

    def _prepare_record(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        ticker = str(raw.get("ticker") or "").strip().upper()
        author = str(raw.get("author") or "").strip()
        if not ticker or not author or ticker not in self.price_map:
            return None
        event_date = _as_date(raw["event_date"])
        prices = self.price_map[ticker]
        dates = pd.DatetimeIndex(prices.index).date
        entry_idx = int(np.searchsorted(dates, event_date, side="right"))
        exit_idx = entry_idx + HOLDING_DAYS - 1
        if entry_idx >= len(prices) or exit_idx >= len(prices):
            return None
        entry_open = float(prices["Open"].iloc[entry_idx])
        exit_close = float(prices["Close"].iloc[exit_idx])
        if entry_open <= 0 or not np.isfinite(exit_close):
            return None
        return {
            "ticker": ticker,
            "author": author,
            "country": raw.get("country") or "Unknown",
            "event_date": event_date,
            "entry_date": _as_date(prices.index[entry_idx]),
            "exit_date": _as_date(prices.index[exit_idx]),
            "long_return_pct": (exit_close / entry_open - 1.0) * 100.0,
            "estimate_up": bool(raw.get("is_estimate_up")),
            "is_7pct_up": bool(raw.get("is_7pct_up")),
            "is_turnover_top10": bool(raw.get("is_turnover_top10")),
            "is_52w_high": bool(raw.get("is_52w_high")),
            "is_52w_low": bool(raw.get("is_52w_low")),
        }

    @staticmethod
    def _completed_outcome(record: dict[str, Any]) -> CompletedOutcome:
        # Estimate-up events are evaluated as long calls; all other catalyst
        # events contribute their short-side directional outcome.
        directional = record["long_return_pct"] if record["estimate_up"] else -record["long_return_pct"]
        return CompletedOutcome(record["author"], record["exit_date"], directional)

    def author_scores_on(self, current_signal_date: date) -> dict[str, float]:
        """Return median-centred, shrunk author scores known strictly before t."""
        eligible = [row for row in self.outcomes if row.exit_date < current_signal_date]
        if not eligible:
            return {}
        all_mean = float(np.mean([row.directional_return_pct for row in eligible]))
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in eligible:
            grouped[row.author].append(row.directional_return_pct)

        raw_scores = {}
        for author, values in grouped.items():
            if len(values) < MIN_AUTHOR_OBSERVATIONS:
                continue
            author_mean = float(np.mean(values))
            raw_scores[author] = (
                len(values) * author_mean + SHRINKAGE_OBSERVATIONS * all_mean
            ) / (len(values) + SHRINKAGE_OBSERVATIONS)
        if not raw_scores:
            return {}
        median = float(np.median(list(raw_scores.values())))
        return {
            author: float(np.clip(score - median, AUTHOR_SCORE_MIN, AUTHOR_SCORE_MAX))
            for author, score in raw_scores.items()
        }

    def factor_on(self, ticker: str, current_signal_date: date) -> dict[str, Any]:
        """Return the active, non-leaking tier for a ticker on signal date t."""
        scores = self.author_scores_on(current_signal_date)
        active = [
            row
            for row in self.records
            if row["ticker"] == ticker
            and row["event_date"] <= current_signal_date <= row["exit_date"]
        ]
        if not active:
            return self._empty_factor()

        candidates = []
        for row in active:
            author_score = scores.get(row["author"], 0.0)
            eligible_long = (
                row["estimate_up"]
                and not row["is_7pct_up"]
                and not row["is_turnover_top10"]
            )
            tier = 0
            if eligible_long and author_score > -2:
                tier = 2 if author_score > 0 else 1
            candidates.append((tier, author_score, row))
        tier, author_score, row = max(candidates, key=lambda item: (item[0], item[1], item[2]["event_date"]))
        return {
            "catalyst_long_tier": tier,
            "author_impact_score": author_score,
            "estimate_up": row["estimate_up"],
            "is_7pct_up": row["is_7pct_up"],
            "is_turnover_top10": row["is_turnover_top10"],
            "is_52w_high": row["is_52w_high"],  # diagnostic only
            "is_52w_low": row["is_52w_low"],    # diagnostic only
            "country": row["country"],
        }

    @staticmethod
    def _empty_factor() -> dict[str, Any]:
        return {
            "catalyst_long_tier": 0,
            "author_impact_score": 0.0,
            "estimate_up": False,
            "is_7pct_up": False,
            "is_turnover_top10": False,
            "is_52w_high": False,
            "is_52w_low": False,
            "country": "Unknown",
        }
