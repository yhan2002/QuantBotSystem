"""Deterministic entry-capacity rules for the portfolio ledger."""
from dataclasses import dataclass
import math

import core_position_mode


@dataclass(frozen=True)
class PortfolioPolicy:
    """Entry-time portfolio limits expressed as fractions of current NAV."""

    initial_nav: float = 100.0
    max_gross: float = 1.0
    max_name_weight: float = 0.05
    long_cap: float | None = None
    short_cap: float | None = None
    position_mode: str = core_position_mode.LONG_SHORT_NEUTRAL
    max_positions: int = 30

    def __post_init__(self):
        try:
            initial_nav = float(self.initial_nav)
        except (TypeError, ValueError) as exc:
            raise ValueError("initial_nav must be a finite positive number") from exc
        if not math.isfinite(initial_nav) or initial_nav <= 0:
            raise ValueError("initial_nav must be finite and positive")
        object.__setattr__(self, "initial_nav", initial_nav)

        for name in ("max_gross", "max_name_weight"):
            try:
                value = float(getattr(self, name))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be finite") from exc
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)

        for name in ("long_cap", "short_cap"):
            raw_value = getattr(self, name)
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be finite") from exc
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)

        try:
            max_positions = int(self.max_positions)
            raw_max_positions = float(self.max_positions)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_positions must be a positive integer") from exc
        if (
            not math.isfinite(raw_max_positions)
            or raw_max_positions != max_positions
            or max_positions <= 0
        ):
            raise ValueError("max_positions must be a positive integer")
        object.__setattr__(self, "max_positions", max_positions)

        mode = core_position_mode.normalize_mode(self.position_mode)
        object.__setattr__(self, "position_mode", mode)

    @property
    def side_caps(self):
        if self.long_cap is not None or self.short_cap is not None:
            return (
                max(0.0, float(self.long_cap or 0.0)),
                max(0.0, float(self.short_cap or 0.0)),
            )
        if self.position_mode == core_position_mode.LONG_ONLY:
            return self.max_gross, 0.0
        if self.position_mode == core_position_mode.SHORT_ONLY:
            return 0.0, self.max_gross
        return self.max_gross / 2.0, self.max_gross / 2.0


def _remaining(capacity, used):
    return max(0.0, float(capacity) - float(used))


def cohort_capacity_scale(
    requested_legs,
    nav,
    current_long,
    current_short,
    current_name_gross,
    policy,
):
    """Return one order-independent scale for a simultaneous entry cohort.

    `requested_legs` contains dictionaries with positive `notional` and a
    signed `direction` (+1 LONG, -1 SHORT). Every leg in the cohort is scaled
    uniformly, preserving strategy score ratios and multi-leg hedge ratios.
    Existing and proposed security exposure is treated gross, not netted.
    """
    nav = float(nav)
    if nav <= 0 or not requested_legs:
        return 0.0

    requested_long = sum(
        float(leg["notional"])
        for leg in requested_legs
        if float(leg["direction"]) > 0
    )
    requested_short = sum(
        float(leg["notional"])
        for leg in requested_legs
        if float(leg["direction"]) < 0
    )
    requested_gross = requested_long + requested_short
    if requested_gross <= 0:
        return 0.0

    long_cap, short_cap = policy.side_caps
    limits = [
        1.0,
        _remaining(policy.max_gross * nav, current_long + current_short)
        / requested_gross,
    ]
    if requested_long:
        limits.append(
            _remaining(long_cap * nav, current_long) / requested_long
        )
    if requested_short:
        limits.append(
            _remaining(short_cap * nav, current_short) / requested_short
        )

    requested_by_name = {}
    for leg in requested_legs:
        ticker = str(leg["ticker"])
        requested_by_name[ticker] = requested_by_name.get(ticker, 0.0) + float(
            leg["notional"]
        )
    for ticker, requested in requested_by_name.items():
        if requested <= 0:
            continue
        used = float(current_name_gross.get(ticker, 0.0))
        limits.append(
            _remaining(policy.max_name_weight * nav, used) / requested
        )

    return max(0.0, min(limits))


def exposure_snapshot(lots, price_by_ticker):
    """Return long, short, gross-by-security values for currently open lots."""
    long_value = 0.0
    short_value = 0.0
    name_gross = {}
    for lot in lots:
        quantity = float(lot["quantity"])
        if abs(quantity) <= 1e-15:
            continue
        ticker = lot["ticker"]
        price = float(price_by_ticker[ticker])
        value = abs(quantity * price)
        name_gross[ticker] = name_gross.get(ticker, 0.0) + value
        if quantity > 0:
            long_value += value
        else:
            short_value += value
    return {
        "long": long_value,
        "short": short_value,
        "gross": long_value + short_value,
        "name_gross": name_gross,
    }
