"""Process-local serialization for scans and market-data mutations."""

from contextlib import contextmanager
from threading import RLock


RUNTIME_OPERATION_LOCK = RLock()


@contextmanager
def serialized_runtime_operation():
    """Run one scan/refresh/admin operation at a time within this process."""
    with RUNTIME_OPERATION_LOCK:
        yield
