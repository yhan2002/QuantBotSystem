"""Directional position modes shared by Beta scans and backtests."""


LONG_ONLY = "LONG_ONLY"
LONG_SHORT_NEUTRAL = "LONG_SHORT_NEUTRAL"
SHORT_ONLY = "SHORT_ONLY"

POSITION_MODE_LABELS = {
    LONG_ONLY: "Long Only",
    LONG_SHORT_NEUTRAL: "Long / Short Neutral",
    SHORT_ONLY: "Short Only",
}


def normalize_mode(mode):
    normalized = str(mode or LONG_SHORT_NEUTRAL).strip().upper()
    if normalized not in POSITION_MODE_LABELS:
        raise ValueError(f"Unsupported position mode: {mode}")
    return normalized


def allows_long(mode):
    return normalize_mode(mode) in (LONG_ONLY, LONG_SHORT_NEUTRAL)


def allows_short(mode):
    return normalize_mode(mode) in (SHORT_ONLY, LONG_SHORT_NEUTRAL)


def side_filter(mode):
    mode = normalize_mode(mode)
    if mode == LONG_ONLY:
        return "LONG"
    if mode == SHORT_ONLY:
        return "SHORT"
    return None


def report_line(mode):
    mode = normalize_mode(mode)
    return f"Position mode: {POSITION_MODE_LABELS[mode]}"
