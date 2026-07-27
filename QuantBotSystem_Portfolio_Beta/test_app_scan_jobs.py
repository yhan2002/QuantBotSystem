import threading
import time
import unittest

import app_scan_jobs


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = app_scan_jobs.JobManager(
            worker_name=f"test-heavy-worker-{id(self)}"
        )

    def tearDown(self):
        self.manager.shutdown(wait=True, timeout=1.0)

    def test_submit_is_immediate_and_worker_is_daemon(self):
        entered = threading.Event()
        release = threading.Event()

        def runner(progress, cancel_event):
            entered.set()
            release.wait(timeout=1.0)
            return "done"

        started = time.perf_counter()
        submission = self.manager.submit(
            "chat-1",
            "scan",
            runner,
            task_type="STRATEGY_SCAN",
            label="Momentum",
        )
        elapsed = time.perf_counter() - started

        self.assertTrue(submission.created)
        self.assertEqual(submission.state, app_scan_jobs.QUEUED)
        self.assertEqual(submission.progress, 1)
        self.assertLess(elapsed, 0.1)
        self.assertTrue(self.manager.worker_is_daemon)
        self.assertTrue(entered.wait(timeout=1.0))

        snapshot = self.manager.get(submission.job_id)
        self.assertEqual(snapshot.task_type, "STRATEGY_SCAN")
        self.assertEqual(snapshot.label, "Momentum")
        self.assertEqual(snapshot.state, app_scan_jobs.RUNNING)
        release.set()
        completed = self.manager.wait(submission.job_id, timeout=1.0)
        self.assertEqual(completed.state, app_scan_jobs.SUCCEEDED)
        self.assertEqual(completed.result, "done")

    def test_duplicate_scope_reuses_active_job_but_other_keys_queue(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking_runner(progress, cancel_event):
            entered.set()
            release.wait(timeout=1.0)

        first = self.manager.submit("chat-1", "same-key", blocking_runner)
        self.assertTrue(entered.wait(timeout=1.0))
        duplicate = self.manager.submit(
            "chat-1",
            "same-key",
            lambda progress, cancel_event: None,
        )
        other = self.manager.submit(
            "chat-1",
            "other-key",
            lambda progress, cancel_event: "other",
        )

        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.job_id, first.job_id)
        self.assertTrue(other.created)
        self.assertEqual(
            self.manager.get(other.job_id).state,
            app_scan_jobs.QUEUED,
        )
        release.set()
        self.assertEqual(
            self.manager.wait(other.job_id, timeout=1.0).state,
            app_scan_jobs.SUCCEEDED,
        )
        replacement = self.manager.submit(
            "chat-1",
            "same-key",
            lambda progress, cancel_event: "replacement",
        )
        self.assertTrue(replacement.created)
        self.assertNotEqual(replacement.job_id, first.job_id)
        self.assertEqual(
            self.manager.wait(replacement.job_id, timeout=1.0).state,
            app_scan_jobs.SUCCEEDED,
        )

    def test_progress_is_monotonic_bucketed_and_100_requires_success(self):
        release = threading.Event()
        progress_snapshots = []

        def runner(progress, cancel_event):
            progress(3, "prepare")
            progress(9, "load")
            progress(4, "load")
            progress(100, "finalize", 10, 10)
            release.wait(timeout=1.0)
            return {"ok": True}

        submission = self.manager.submit(
            "chat",
            "progress",
            runner,
            on_progress=progress_snapshots.append,
        )
        self.assertTrue(
            self._wait_until(
                lambda: self.manager.get(submission.job_id).progress == 95
            )
        )
        running = self.manager.get(submission.job_id)
        self.assertEqual(running.state, app_scan_jobs.RUNNING)
        self.assertEqual(running.progress, 95)
        self.assertEqual(running.current, 10)
        self.assertEqual(running.total, 10)

        published = [snapshot.progress for snapshot in progress_snapshots]
        self.assertEqual(published, sorted(published))
        self.assertIn(1, published)
        self.assertIn(5, published)
        self.assertIn(95, published)
        self.assertNotIn(100, published)

        release.set()
        completed = self.manager.wait(submission.job_id, timeout=1.0)
        self.assertEqual(completed.progress, 100)
        self.assertEqual(completed.state, app_scan_jobs.SUCCEEDED)

    def test_runner_and_callback_errors_are_isolated(self):
        def broken_callback(snapshot):
            raise LookupError("ui unavailable")

        class BrokenPercent:
            def __float__(self):
                raise RuntimeError("bad progress value")

        def failing_runner(progress, cancel_event):
            progress(25, "work")
            raise ValueError("runner failed")

        failure = self.manager.submit(
            "chat",
            "failure",
            failing_runner,
            on_progress=broken_callback,
            on_error=broken_callback,
        )
        failed = self.manager.wait(failure.job_id, timeout=1.0)

        self.assertEqual(failed.state, app_scan_jobs.FAILED)
        self.assertEqual(failed.progress, 25)
        self.assertEqual(failed.error, "ValueError: runner failed")
        self.assertGreaterEqual(len(self.manager.get(failure.job_id).callback_errors), 2)

        success = self.manager.submit(
            "chat",
            "success",
            lambda progress, cancel_event: 42,
            on_complete=broken_callback,
        )
        succeeded = self.manager.wait(success.job_id, timeout=1.0)
        self.assertEqual(succeeded.state, app_scan_jobs.SUCCEEDED)
        self.assertEqual(succeeded.result, 42)
        self.assertTrue(
            self._wait_until(
                lambda: len(
                    self.manager.get(success.job_id).callback_errors
                )
                == 1
            )
        )

        progress_error = self.manager.submit(
            "chat",
            "progress-error",
            lambda progress, cancel_event: (
                progress(BrokenPercent()),
                "completed despite progress callback failure",
            )[1],
        )
        progress_error_result = self.manager.wait(
            progress_error.job_id,
            timeout=1.0,
        )
        self.assertEqual(
            progress_error_result.state,
            app_scan_jobs.SUCCEEDED,
        )
        self.assertEqual(len(progress_error_result.callback_errors), 1)

    def test_cooperative_running_cancel_and_immediate_queued_cancel(self):
        entered = threading.Event()
        release = threading.Event()

        def running_runner(progress, cancel_event):
            entered.set()
            cancel_event.wait(timeout=1.0)
            progress(100, "should not publish")
            return "ignored"

        running = self.manager.submit("chat", "running", running_runner)
        self.assertTrue(entered.wait(timeout=1.0))
        queued = self.manager.submit(
            "chat",
            "queued",
            lambda progress, cancel_event: release.wait(timeout=1.0),
        )

        queued_cancelled = self.manager.cancel(queued.job_id)
        self.assertEqual(queued_cancelled.state, app_scan_jobs.CANCELLED)
        self.assertEqual(queued_cancelled.progress, 1)
        self.assertIsNone(self.manager.get_active("chat", "queued"))

        requested = self.manager.cancel(running.job_id)
        self.assertTrue(requested.cancel_requested)
        cancelled = self.manager.wait(running.job_id, timeout=1.0)
        self.assertEqual(cancelled.state, app_scan_jobs.CANCELLED)
        self.assertLess(cancelled.progress, 100)
        self.assertIsNone(cancelled.result)

    def test_snapshots_copy_metadata_and_are_safe_for_parallel_reads(self):
        release = threading.Event()
        metadata = {"source": "admin"}
        submission = self.manager.submit(
            "chat",
            "metadata",
            lambda progress, cancel_event: release.wait(timeout=1.0),
            task_type="MARKET_REFRESH",
            metadata=metadata,
        )
        metadata["source"] = "changed"

        errors = []

        def reader():
            try:
                for _ in range(200):
                    snapshot = self.manager.get(submission.job_id)
                    self.assertIn(
                        snapshot.state,
                        app_scan_jobs.ACTIVE_STATES
                        | app_scan_jobs.TERMINAL_STATES,
                    )
            except BaseException as exc:
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in readers:
            thread.start()
        for thread in readers:
            thread.join(timeout=1.0)

        self.assertFalse(errors)
        self.assertEqual(
            self.manager.get(submission.job_id).metadata["source"],
            "admin",
        )
        with self.assertRaises(TypeError):
            self.manager.get(submission.job_id).metadata["new"] = "value"
        release.set()
        self.manager.wait(submission.job_id, timeout=1.0)

    def test_terminal_history_is_bounded_without_pruning_active_jobs(self):
        manager = app_scan_jobs.JobManager(
            worker_name="test-bounded-history",
            history_limit=2,
        )
        try:
            completed_ids = []
            for key in ("one", "two", "three"):
                submission = manager.submit(
                    "chat",
                    key,
                    lambda progress, cancel_event, value=key: value,
                )
                completed = manager.wait(submission.job_id, timeout=1.0)
                self.assertEqual(completed.state, app_scan_jobs.SUCCEEDED)
                completed_ids.append(submission.job_id)

            self.assertIsNone(manager.get(completed_ids[0]))
            self.assertIsNotNone(manager.get(completed_ids[1]))
            self.assertIsNotNone(manager.get(completed_ids[2]))

            release = threading.Event()
            running = manager.submit(
                "chat",
                "running",
                lambda progress, cancel_event: release.wait(timeout=1.0),
            )
            queued = manager.submit(
                "chat",
                "queued",
                lambda progress, cancel_event: "queued",
            )
            self.assertTrue(
                self._wait_until(
                    lambda: manager.get(running.job_id).state
                    == app_scan_jobs.RUNNING
                )
            )
            self.assertIsNotNone(manager.get(running.job_id))
            self.assertIsNotNone(manager.get(queued.job_id))
            release.set()
            manager.wait(queued.job_id, timeout=1.0)
        finally:
            manager.shutdown(wait=True, timeout=1.0)

    @staticmethod
    def _wait_until(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()


if __name__ == "__main__":
    unittest.main()
