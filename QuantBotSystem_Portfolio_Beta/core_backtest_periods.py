"""Fixed selection and rolling out-of-sample windows shared by Beta strategies."""

from datetime import date


HISTORY_PERIOD = "4y"
SELECTION_START = "2024-01-01"
SELECTION_END = "2025-12-31"
OOS_START = "2026-01-01"
BENCHMARK_MAX_STALE_CALENDAR_DAYS = 7


def selection_window():
    return {"evaluation_start": SELECTION_START, "evaluation_end": SELECTION_END}


def oos_end(as_of=None):
    return (as_of or date.today()).isoformat()


def oos_window(as_of=None):
    return {"evaluation_start": OOS_START, "evaluation_end": oos_end(as_of)}
