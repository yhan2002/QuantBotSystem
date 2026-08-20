"""Entry execution benchmarks shared by Beta strategy backtests."""

import pandas as pd


NEXT_OPEN = "NEXT_OPEN"
NEXT_DAY_OHLC4 = "NEXT_DAY_OHLC4"
ADVERSE_GAP_FILTER = "ADVERSE_GAP_FILTER"
DEFAULT_ADVERSE_GAP_LIMIT_PCT = 2.0
SUPPORTED_MODES = (NEXT_OPEN, NEXT_DAY_OHLC4, ADVERSE_GAP_FILTER)


def normalize_fixed_exit_sessions(value):
    if value is None:
        return None
    try:
        sessions = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("fixed_exit_sessions must be a positive integer") from exc
    if isinstance(value, bool) or sessions < 1 or numeric != sessions:
        raise ValueError("fixed_exit_sessions must be a positive integer")
    return sessions


def normalize_mode(mode):
    normalized = str(mode or NEXT_OPEN).strip().upper()
    if normalized not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported entry execution mode: {mode}")
    return normalized


def resolve_entry(
    prices,
    signal_idx,
    side,
    mode=NEXT_OPEN,
    adverse_gap_limit_pct=DEFAULT_ADVERSE_GAP_LIMIT_PCT,
):
    """Resolve a close-known signal into a next-session execution benchmark.

    NEXT_DAY_OHLC4 is an end-of-session execution benchmark, not observed VWAP.
    It therefore cannot use the execution day's high/low for strategy exits.
    """
    mode = normalize_mode(mode)
    entry_idx = signal_idx + 1
    if signal_idx < 0 or entry_idx >= len(prices):
        return None

    signal_close = float(prices["Close"].iloc[signal_idx])
    next_open = float(prices["Open"].iloc[entry_idx])
    if signal_close <= 0 or next_open <= 0:
        return None

    direction = 1.0 if side == "LONG" else -1.0
    signal_to_open_return_pct = direction * (next_open / signal_close - 1.0) * 100.0
    if (
        mode == ADVERSE_GAP_FILTER
        and signal_to_open_return_pct > float(adverse_gap_limit_pct)
    ):
        return None

    if mode == NEXT_DAY_OHLC4:
        row = prices.iloc[entry_idx]
        values = [row.get(name) for name in ("Open", "High", "Low", "Close")]
        if any(not pd.notna(value) for value in values):
            return None
        price = sum(float(value) for value in values) / 4.0
        allow_entry_day_exit = False
    else:
        price = next_open
        allow_entry_day_exit = True

    if price <= 0:
        return None
    signal_to_fill_return_pct = direction * (price / signal_close - 1.0) * 100.0
    return {
        "mode": mode,
        "entry_idx": entry_idx,
        "price": price,
        "signal_close": signal_close,
        "next_open": next_open,
        "signal_to_open_return_pct": signal_to_open_return_pct,
        "signal_to_fill_return_pct": signal_to_fill_return_pct,
        "allow_entry_day_exit": allow_entry_day_exit,
    }
