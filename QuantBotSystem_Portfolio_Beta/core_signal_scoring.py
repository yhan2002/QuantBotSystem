"""Deterministic cross-sectional ranking and requested position weights."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Sequence

from core_signal_model import Signal


DEFAULT_MAX_NAME_WEIGHT = 0.05
DEFAULT_WINSOR_LIMITS = (0.05, 0.95)


@dataclass(frozen=True, slots=True)
class ScoredSignal:
    """A signal annotated with its supplied cross-sectional score and sizing."""

    signal: Signal
    selected_score: float
    winsorized_score: float
    percentile_rank: float
    rank_score: float
    requested_weight: float

    @property
    def winsorized_magnitude(self) -> float:
        return abs(self.winsorized_score)


def _validate_max_name_weight(value: float) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_name_weight must be a finite number") from exc
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        raise ValueError("max_name_weight must be between 0 and 1")
    return weight


def _validate_winsor_limits(limits: Sequence[float]) -> tuple[float, float]:
    if len(limits) != 2:
        raise ValueError("winsor_limits must contain lower and upper quantiles")
    try:
        lower, upper = (float(value) for value in limits)
    except (TypeError, ValueError) as exc:
        raise ValueError("winsor_limits must be numeric") from exc
    if not all(math.isfinite(value) for value in (lower, upper)):
        raise ValueError("winsor_limits must be finite")
    if not 0.0 <= lower <= upper <= 1.0:
        raise ValueError("winsor_limits must satisfy 0 <= lower <= upper <= 1")
    return lower, upper


def _quantile(sorted_values: Sequence[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a quantile for an empty cohort")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return (
        sorted_values[lower_index] * (1.0 - fraction)
        + sorted_values[upper_index] * fraction
    )


def _winsorize(values: Sequence[float], limits: tuple[float, float]) -> list[float]:
    ordered = sorted(values)
    lower = _quantile(ordered, limits[0])
    upper = _quantile(ordered, limits[1])
    return [min(max(value, lower), upper) for value in values]


def _percentile_ranks(values: Sequence[float]) -> list[float]:
    """Return average-tie, one-based ranks divided by cohort size."""
    if not values:
        return []
    ordered = sorted(values)
    rank_by_value = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end] == ordered[start]:
            end += 1
        average_one_based_rank = ((start + 1) + end) / 2.0
        rank_by_value[ordered[start]] = average_one_based_rank / len(ordered)
        start = end
    return [rank_by_value[value] for value in values]


def score_signal_cohort(
    candidates: Iterable[tuple[Signal, float]],
    *,
    max_name_weight: float = DEFAULT_MAX_NAME_WEIGHT,
    winsor_limits: Sequence[float] = DEFAULT_WINSOR_LIMITS,
) -> list[ScoredSignal]:
    """Score one entry cohort using already-selected neutralization scores.

    The caller supplies ``(Signal, selected_score)`` pairs. A selected score
    must be positive for LONG or negative for SHORT; zero and sign-inconsistent
    candidates are rejected. Magnitudes are winsorized and percentile-ranked
    independently for each side. No data access, ML, or probability estimate is
    performed here.
    """

    max_weight = _validate_max_name_weight(max_name_weight)
    limits = _validate_winsor_limits(winsor_limits)
    accepted: list[tuple[Signal, float]] = []
    for signal, selected_score in candidates:
        if not isinstance(signal, Signal):
            raise TypeError("each candidate must contain a Signal")
        try:
            score = float(selected_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("selected_score must be a finite number") from exc
        if not math.isfinite(score):
            raise ValueError("selected_score must be a finite number")
        if (signal.side == "LONG" and score <= 0.0) or (
            signal.side == "SHORT" and score >= 0.0
        ):
            continue
        accepted.append((signal, score))

    scored_by_index: dict[int, ScoredSignal] = {}
    by_side: dict[str, list[tuple[int, Signal, float]]] = defaultdict(list)
    for index, (signal, score) in enumerate(accepted):
        by_side[signal.side].append((index, signal, score))

    for side in ("LONG", "SHORT"):
        side_candidates = by_side.get(side, [])
        if not side_candidates:
            continue
        magnitudes = [abs(score) for _, _, score in side_candidates]
        winsorized_magnitudes = _winsorize(magnitudes, limits)
        percentile_ranks = _percentile_ranks(winsorized_magnitudes)
        direction = 1.0 if side == "LONG" else -1.0
        for (index, signal, selected_score), magnitude, percentile_rank in zip(
            side_candidates, winsorized_magnitudes, percentile_ranks
        ):
            rank_score = direction * percentile_rank
            scored_by_index[index] = ScoredSignal(
                signal=signal,
                selected_score=selected_score,
                winsorized_score=direction * magnitude,
                percentile_rank=percentile_rank,
                rank_score=rank_score,
                requested_weight=max_weight * abs(rank_score),
            )

    return [scored_by_index[index] for index in range(len(accepted))]


def annotate_trades_by_entry_date(
    trades: Iterable[Mapping[str, object]],
    *,
    strategy: str = "UNSPECIFIED",
    max_name_weight: float = DEFAULT_MAX_NAME_WEIGHT,
    winsor_limits: Sequence[float] = DEFAULT_WINSOR_LIMITS,
) -> list[dict[str, object]]:
    """Copy, sign-filter, and score trade dictionaries within entry-date cohorts.

    ``neutralized_score`` is selected when it is present and non-null;
    otherwise ``signal_score`` is used. The returned rows retain every original
    field, including ``setup`` and ``structure``, and add deterministic score
    annotations. Rejected rows are omitted and the caller's dictionaries are
    never mutated.
    """

    indexed_rows: list[tuple[int, Mapping[str, object], Signal, float]] = []
    cohorts: dict[object, list[tuple[Signal, float]]] = defaultdict(list)
    for index, trade in enumerate(trades):
        if not isinstance(trade, Mapping):
            raise TypeError("each trade must be a mapping")
        entry_date = trade.get("entry_date")
        if entry_date is None or str(entry_date).strip() == "":
            raise ValueError("each trade must have an entry_date")

        selected_value = trade.get("neutralized_score")
        if selected_value is None:
            selected_value = trade.get("signal_score")
        if selected_value is None:
            raise ValueError("trade must have neutralized_score or signal_score")

        raw_value = trade.get("signal_score")
        if raw_value is None:
            raw_value = selected_value
        signal = Signal(
            strategy=trade.get("strategy") or strategy,
            ticker=trade.get("ticker"),
            side=trade.get("side"),
            signal_date=trade.get("signal_date") or entry_date,
            raw_score=raw_value,
            status=trade.get("status"),
            setup=trade.get("setup"),
            structure=trade.get("structure"),
            metadata=trade.get("metadata") or {},
            entry_execution=trade.get("entry_execution"),
        )
        try:
            selected_score = float(selected_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("selected score must be a finite number") from exc
        indexed_rows.append((index, trade, signal, selected_score))
        cohorts[entry_date].append((signal, selected_score))

    scored_by_signal_id: dict[int, ScoredSignal] = {}
    for candidates in cohorts.values():
        for scored in score_signal_cohort(
            candidates,
            max_name_weight=max_name_weight,
            winsor_limits=winsor_limits,
        ):
            scored_by_signal_id[id(scored.signal)] = scored

    annotated_by_index: dict[int, dict[str, object]] = {}
    for index, trade, signal, _ in indexed_rows:
        scored = scored_by_signal_id.get(id(signal))
        if scored is None:
            continue
        row = dict(trade)
        row.update(
            {
                "raw_score": signal.raw_score,
                "selected_score": scored.selected_score,
                "winsorized_score": scored.winsorized_score,
                "percentile_rank": scored.percentile_rank,
                "rank_score": scored.rank_score,
                "requested_weight": scored.requested_weight,
            }
        )
        annotated_by_index[index] = row

    return [
        annotated_by_index[index]
        for index in range(len(indexed_rows))
        if index in annotated_by_index
    ]


def annotate_trade_scores_by_entry_date(
    trades: Iterable[Mapping[str, object]],
    **kwargs: object,
) -> list[dict[str, object]]:
    """Compatibility alias with an explicit scoring-oriented name."""

    return annotate_trades_by_entry_date(trades, **kwargs)
