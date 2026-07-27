"""Process-local background queue for long-running QuantBot operations.

The manager is intentionally independent of Telegram.  A caller submits any
heavy runner (strategy scan, refresh, backfill, metadata download, and so on)
and may translate immutable job snapshots into its own UI.

Runner contract::

    result = runner(progress_callback, cancel_event)

``progress_callback(percent, stage=None, current=None, total=None)`` is safe to
call frequently.  Percentages are published in monotonic 5% buckets, with 1%
reserved for the initial queued/running state and 100% reserved for a
successfully completed runner.  Cancellation is cooperative: runners should
check ``cancel_event.is_set()`` and return promptly.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from queue import Queue
from threading import Condition, Event, RLock, Thread
from time import time
from types import MappingProxyType
from typing import Any, Callable, Hashable, Mapping
from uuid import uuid4


QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
ACTIVE_STATES = frozenset({QUEUED, RUNNING})


class JobCancelled(BaseException):
    """Internal control signal that progress adapters must not swallow."""


ProgressCallback = Callable[[float, str | None, int | None, int | None], None]
Runner = Callable[[ProgressCallback, Event], Any]
SnapshotCallback = Callable[["JobSnapshot"], None]


@dataclass(frozen=True)
class JobSnapshot:
    """Immutable, thread-safe view of a queued or completed job."""

    job_id: str
    chat_id: Hashable
    key: Hashable
    task_type: str
    label: str
    state: str
    progress: int
    stage: str
    current: int | None
    total: int | None
    result: Any
    error: str | None
    cancel_requested: bool
    callback_errors: tuple[str, ...]
    created_at: float
    started_at: float | None
    finished_at: float | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class JobSubmission:
    """Result returned immediately by :meth:`JobManager.submit`."""

    job_id: str
    state: str
    progress: int
    created: bool


@dataclass
class _JobRecord:
    job_id: str
    chat_id: Hashable
    key: Hashable
    task_type: str
    label: str
    runner: Runner
    on_progress: SnapshotCallback | None
    on_complete: SnapshotCallback | None
    on_error: SnapshotCallback | None
    metadata: dict[str, Any]
    state: str = QUEUED
    progress: int = 1
    stage: str = "Queued"
    current: int | None = None
    total: int | None = None
    result: Any = None
    error: str | None = None
    cancel_event: Event = field(default_factory=Event)
    callback_errors: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time)
    started_at: float | None = None
    finished_at: float | None = None


class JobManager:
    """One daemon worker and one FIFO queue shared by all heavy task types."""

    _STOP = object()

    def __init__(
        self,
        *,
        worker_name: str = "quantbot-heavy-job-worker",
        history_limit: int = 256,
    ):
        if history_limit < 1:
            raise ValueError("history_limit must be at least 1")
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._queue: Queue[str | object] = Queue()
        self._jobs: dict[str, _JobRecord] = {}
        self._active_by_scope: dict[tuple[Hashable, Hashable], str] = {}
        self._terminal_history: deque[str] = deque()
        self._history_limit = int(history_limit)
        self._closed = False
        self._worker = Thread(
            target=self._worker_loop,
            name=worker_name,
            daemon=True,
        )
        self._worker.start()

    @property
    def worker_is_daemon(self) -> bool:
        return self._worker.daemon

    def submit(
        self,
        chat_id: Hashable,
        key: Hashable,
        runner: Runner,
        *,
        task_type: str = "GENERIC",
        label: str = "",
        metadata: Mapping[str, Any] | None = None,
        on_progress: SnapshotCallback | None = None,
        on_complete: SnapshotCallback | None = None,
        on_error: SnapshotCallback | None = None,
    ) -> JobSubmission:
        """Enqueue a runner, or return the existing active chat/key job.

        Duplicate prevention is scoped by ``(chat_id, key)``.  Different keys
        still share the same global worker and therefore execute serially.
        """

        if not callable(runner):
            raise TypeError("runner must be callable")
        scope = (chat_id, key)
        with self._condition:
            if self._closed:
                raise RuntimeError("job manager is shut down")
            active_job_id = self._active_by_scope.get(scope)
            if active_job_id is not None:
                active = self._jobs[active_job_id]
                if active.state in ACTIVE_STATES:
                    return JobSubmission(
                        job_id=active.job_id,
                        state=active.state,
                        progress=active.progress,
                        created=False,
                    )
                self._active_by_scope.pop(scope, None)

            job_id = uuid4().hex
            job = _JobRecord(
                job_id=job_id,
                chat_id=chat_id,
                key=key,
                task_type=str(task_type or "GENERIC"),
                label=str(label or ""),
                runner=runner,
                on_progress=on_progress,
                on_complete=on_complete,
                on_error=on_error,
                metadata=dict(metadata or {}),
            )
            self._jobs[job_id] = job
            self._active_by_scope[scope] = job_id
            self._queue.put(job_id)
            self._condition.notify_all()
            return JobSubmission(
                job_id=job_id,
                state=QUEUED,
                progress=1,
                created=True,
            )

    def get(self, job_id: str) -> JobSnapshot | None:
        """Return an immutable snapshot, or ``None`` for an unknown id."""

        with self._lock:
            job = self._jobs.get(job_id)
            return self._snapshot(job) if job is not None else None

    def get_active(
        self,
        chat_id: Hashable,
        key: Hashable,
    ) -> JobSnapshot | None:
        """Return the active job for a duplicate-prevention scope."""

        with self._lock:
            job_id = self._active_by_scope.get((chat_id, key))
            if job_id is None:
                return None
            job = self._jobs.get(job_id)
            if job is None or job.state not in ACTIVE_STATES:
                return None
            return self._snapshot(job)

    def cancel(self, job_id: str) -> JobSnapshot | None:
        """Request cooperative cancellation.

        A queued job is cancelled immediately.  A running job remains RUNNING
        until its runner observes the event and returns; it is then finalized
        as CANCELLED and never advances to 100%.
        """

        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state in TERMINAL_STATES:
                return self._snapshot(job)

            job.cancel_event.set()
            if job.state == QUEUED:
                job.state = CANCELLED
                job.stage = "Cancelled"
                job.finished_at = time()
                self._release_scope(job)
                self._remember_terminal(job)
            self._condition.notify_all()
            return self._snapshot(job)

    def wait(
        self,
        job_id: str,
        timeout: float | None = None,
    ) -> JobSnapshot | None:
        """Wait for a terminal state, primarily for local tests and tooling."""

        deadline = None if timeout is None else time() + max(0.0, timeout)
        with self._condition:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return None
                if job.state in TERMINAL_STATES:
                    return self._snapshot(job)
                remaining = None if deadline is None else deadline - time()
                if remaining is not None and remaining <= 0:
                    return self._snapshot(job)
                self._condition.wait(remaining)

    def shutdown(self, *, wait: bool = False, timeout: float | None = None):
        """Stop accepting work and ask the daemon worker to exit.

        Already queued jobs remain ahead of the stop marker.  Production
        processes normally rely on daemon semantics and need not call this.
        """

        with self._condition:
            if not self._closed:
                self._closed = True
                self._queue.put(self._STOP)
                self._condition.notify_all()
        if wait:
            self._worker.join(timeout=timeout)

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                self._run_job(str(item))
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str):
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.state == CANCELLED:
                self._condition.notify_all()
                return
            job.state = RUNNING
            job.stage = "Running"
            job.started_at = time()
            self._condition.notify_all()

        self._safe_callback(job, job.on_progress)

        def progress(
            percent: float,
            stage: str | None = None,
            current: int | None = None,
            total: int | None = None,
        ):
            try:
                self._record_progress(
                    job_id,
                    percent,
                    stage,
                    current,
                    total,
                )
            except BaseException as exc:
                self._record_callback_error(job, exc)

        try:
            result = job.runner(progress, job.cancel_event)
        except BaseException as exc:
            with self._condition:
                if job.cancel_event.is_set():
                    job.state = CANCELLED
                    job.stage = "Cancelled"
                else:
                    job.state = FAILED
                    job.stage = "Failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = time()
                self._release_scope(job)
                self._remember_terminal(job)
                snapshot = self._snapshot(job)
                self._condition.notify_all()
            if snapshot.state == FAILED:
                self._safe_callback(job, job.on_error, snapshot)
            return

        with self._condition:
            if job.cancel_event.is_set():
                job.state = CANCELLED
                job.stage = "Cancelled"
            else:
                job.state = SUCCEEDED
                job.stage = "Completed"
                job.progress = 100
                job.result = result
            job.finished_at = time()
            self._release_scope(job)
            self._remember_terminal(job)
            snapshot = self._snapshot(job)
            self._condition.notify_all()

        if snapshot.state == SUCCEEDED:
            self._safe_callback(job, job.on_complete, snapshot)

    def _record_progress(
        self,
        job_id: str,
        percent: float,
        stage: str | None,
        current: int | None,
        total: int | None,
    ):
        try:
            numeric_percent = float(percent)
        except (TypeError, ValueError):
            return
        if numeric_percent != numeric_percent:  # NaN
            return

        bucket = 1 if numeric_percent < 5 else int(numeric_percent // 5) * 5
        bucket = max(1, min(95, bucket))
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.state != RUNNING:
                return
            previous_progress = job.progress
            previous_stage = job.stage
            job.progress = max(job.progress, bucket)
            if stage is not None:
                job.stage = str(stage)
            job.current = current
            job.total = total
            should_publish = (
                job.progress > previous_progress
                or job.stage != previous_stage
            )
            snapshot = self._snapshot(job)
            self._condition.notify_all()

        if should_publish:
            self._safe_callback(job, job.on_progress, snapshot)

    def _safe_callback(
        self,
        job: _JobRecord,
        callback: SnapshotCallback | None,
        snapshot: JobSnapshot | None = None,
    ):
        if callback is None:
            return
        try:
            callback(snapshot or self.get(job.job_id))
        except BaseException as exc:
            self._record_callback_error(job, exc)

    def _record_callback_error(
        self,
        job: _JobRecord,
        exc: BaseException,
    ):
        with self._condition:
            job.callback_errors.append(f"{type(exc).__name__}: {exc}")
            self._condition.notify_all()

    def _release_scope(self, job: _JobRecord):
        scope = (job.chat_id, job.key)
        if self._active_by_scope.get(scope) == job.job_id:
            self._active_by_scope.pop(scope, None)

    def _remember_terminal(self, job: _JobRecord):
        """Retain only the newest terminal records; never prune active work."""

        if job.state not in TERMINAL_STATES:
            return
        self._terminal_history.append(job.job_id)
        while len(self._terminal_history) > self._history_limit:
            expired_job_id = self._terminal_history.popleft()
            expired = self._jobs.get(expired_job_id)
            if expired is not None and expired.state in TERMINAL_STATES:
                self._jobs.pop(expired_job_id, None)

    @staticmethod
    def _snapshot(job: _JobRecord) -> JobSnapshot:
        return JobSnapshot(
            job_id=job.job_id,
            chat_id=job.chat_id,
            key=job.key,
            task_type=job.task_type,
            label=job.label,
            state=job.state,
            progress=job.progress,
            stage=job.stage,
            current=job.current,
            total=job.total,
            result=job.result,
            error=job.error,
            cancel_requested=job.cancel_event.is_set(),
            callback_errors=tuple(job.callback_errors),
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            metadata=MappingProxyType(dict(job.metadata)),
        )


# One shared queue for every heavyweight task in this process.
DEFAULT_JOB_MANAGER = JobManager()


def submit_job(*args, **kwargs) -> JobSubmission:
    return DEFAULT_JOB_MANAGER.submit(*args, **kwargs)


def get_job(job_id: str) -> JobSnapshot | None:
    return DEFAULT_JOB_MANAGER.get(job_id)


def get_active_job(chat_id: Hashable, key: Hashable) -> JobSnapshot | None:
    return DEFAULT_JOB_MANAGER.get_active(chat_id, key)


def cancel_job(job_id: str) -> JobSnapshot | None:
    return DEFAULT_JOB_MANAGER.cancel(job_id)
