"""Fixed in-sample and out-of-sample windows shared by all Beta strategies."""


HISTORY_PERIOD = "4y"
SELECTION_START = "2024-01-01"
SELECTION_END = "2025-12-31"
OOS_START = "2026-01-01"
OOS_END = "2026-07-31"
BENCHMARK_MAX_STALE_CALENDAR_DAYS = 7


def selection_window():
    return {"evaluation_start": SELECTION_START, "evaluation_end": SELECTION_END}


def oos_window():
    return {"evaluation_start": OOS_START, "evaluation_end": OOS_END}
