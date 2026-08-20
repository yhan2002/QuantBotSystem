"""Immutable strategy signal model shared by portfolio components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import math
from types import MappingProxyType
from typing import Any, Mapping


VALID_SIDES = frozenset({"LONG", "SHORT"})


def _empty_metadata() -> Mapping[str, Any]:
    return MappingProxyType({})


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} cannot be blank")
    return text


@dataclass(frozen=True, slots=True)
class Signal:
    """A close-known directional signal before portfolio sizing.

    ``raw_score`` is the strategy-native score. Cross-sectional
    neutralization and ranking belong in ``core_signal_scoring`` so this model
    does not depend on a database or a particular neutralization method.
    """

    strategy: str
    ticker: str
    side: str
    signal_date: str
    raw_score: float
    status: str | None = None
    setup: str | None = None
    structure: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=_empty_metadata)
    entry_execution: str | None = None

    def __post_init__(self) -> None:
        if self.strategy is None:
            raise ValueError("strategy cannot be blank")
        if self.ticker is None:
            raise ValueError("ticker cannot be blank")
        strategy = str(self.strategy).strip()
        ticker = str(self.ticker).strip().upper()
        side = str(self.side).strip().upper()
        if not strategy:
            raise ValueError("strategy cannot be blank")
        if not ticker:
            raise ValueError("ticker cannot be blank")
        if side not in VALID_SIDES:
            raise ValueError(f"side must be LONG or SHORT, got {self.side!r}")

        signal_date = self.signal_date
        if signal_date is None:
            raise ValueError("signal_date cannot be blank")
        if isinstance(signal_date, datetime):
            signal_date = signal_date.date().isoformat()
        elif isinstance(signal_date, date):
            signal_date = signal_date.isoformat()
        else:
            signal_date = str(signal_date).strip()
        if not signal_date:
            raise ValueError("signal_date cannot be blank")

        try:
            raw_score = float(self.raw_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("raw_score must be a finite number") from exc
        if not math.isfinite(raw_score):
            raise ValueError("raw_score must be a finite number")

        if self.metadata is None:
            metadata = {}
        elif isinstance(self.metadata, Mapping):
            metadata = dict(self.metadata)
        else:
            raise TypeError("metadata must be a mapping")

        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "signal_date", signal_date)
        object.__setattr__(self, "raw_score", raw_score)
        object.__setattr__(self, "status", _optional_text(self.status, "status"))
        object.__setattr__(self, "setup", _optional_text(self.setup, "setup"))
        object.__setattr__(
            self, "structure", _optional_text(self.structure, "structure")
        )
        object.__setattr__(self, "metadata", MappingProxyType(metadata))
        object.__setattr__(
            self,
            "entry_execution",
            _optional_text(self.entry_execution, "entry_execution"),
        )
