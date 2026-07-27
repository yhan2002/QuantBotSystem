import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import RUN_telegram_bot_server
import app_daily_market_scheduler


class TelegramServerRuntimeSafetyTests(unittest.TestCase):
    def test_progress_text_matches_strategy_loading_layout(self):
        snapshot = SimpleNamespace(
            label="Momentum Scan",
            progress=50,
            stage="IS 백테스트",
            current=203,
            total=399,
            started_at=246,
            created_at=246,
        )

        with patch.object(
            RUN_telegram_bot_server.time,
            "time",
            return_value=1000,
        ):
            text = RUN_telegram_bot_server.format_job_progress(snapshot)

        self.assertIn("██████████░░░░░░░░░░ 50%", text)
        self.assertIn("단계: IS 백테스트", text)
        self.assertIn("진행: 203 / 399종목", text)
        self.assertIn("경과시간: 12분 34초", text)

    def test_is_stage_maps_203_of_399_to_about_50_percent(self):
        emitted = []
        adapter = RUN_telegram_bot_server._scan_progress_adapter(
            lambda *args: emitted.append(args),
            threading.Event(),
        )

        adapter("IS_BACKTEST", 203, 399, "AAA", True)

        self.assertEqual(1, len(emitted))
        self.assertAlmostEqual(50.263, emitted[0][0], places=3)
        self.assertEqual("IS 백테스트", emitted[0][1])
        self.assertEqual((203, 399), emitted[0][2:])

    def test_data_load_advances_after_preparing(self):
        emitted = []
        adapter = RUN_telegram_bot_server._scan_progress_adapter(
            lambda *args: emitted.append(args),
            threading.Event(),
        )

        adapter("PREPARING", 1, 1)
        adapter("DATA_LOAD", 200, 400, "AAA", True)

        self.assertEqual(5.0, emitted[0][0])
        self.assertEqual(7.5, emitted[1][0])

    def test_download_stage_uses_shared_five_to_ninety_five_range(self):
        emitted = []
        adapter = RUN_telegram_bot_server._download_progress_adapter(
            lambda *args: emitted.append(args),
            threading.Event(),
            "최신 가격 캐시 다운로드",
        )

        adapter(50, 100, "AAA")

        self.assertEqual(
            [(50.0, "최신 가격 캐시 다운로드", 50, 100)],
            emitted,
        )

    def test_progress_adapters_propagate_cooperative_cancellation(self):
        cancelled = threading.Event()
        cancelled.set()

        scan_adapter = RUN_telegram_bot_server._scan_progress_adapter(
            lambda *args: None,
            cancelled,
        )
        download_adapter = RUN_telegram_bot_server._download_progress_adapter(
            lambda *args: None,
            cancelled,
            "다운로드",
        )

        with self.assertRaises(
            RUN_telegram_bot_server.app_scan_jobs.JobCancelled
        ):
            scan_adapter("CURRENT_SCAN", 1, 2, "AAA", True)
        with self.assertRaises(
            RUN_telegram_bot_server.app_scan_jobs.JobCancelled
        ):
            download_adapter(1, 2, "AAA")

    def test_submit_does_not_apply_a_racy_post_submit_progress_edit(self):
        submission = SimpleNamespace(
            job_id="fast-job",
            created=True,
            state="SUCCEEDED",
            progress=100,
        )

        with patch.object(
            RUN_telegram_bot_server.app_scan_jobs,
            "submit_job",
            return_value=submission,
        ), patch.object(
            RUN_telegram_bot_server.app_scan_jobs,
            "get_job",
        ) as get_job:
            result = RUN_telegram_bot_server.submit_background_operation(
                "chat",
                123,
                label="Fast job",
                task_type="TEST",
                runner=lambda progress, cancel_event: "done",
                on_result=lambda value: None,
            )

        self.assertIs(result, submission)
        get_job.assert_not_called()

    def test_progress_edit_removes_the_old_inline_menu(self):
        snapshot = SimpleNamespace(
            label="Momentum Scan",
            progress=25,
            stage="현재 후보 스캔",
            current=10,
            total=100,
            started_at=1000,
            created_at=1000,
        )

        with patch.object(
            RUN_telegram_bot_server.time,
            "time",
            return_value=1001,
        ), patch.object(
            RUN_telegram_bot_server.bot,
            "edit_message_text",
        ) as edit:
            RUN_telegram_bot_server._edit_job_progress(
                "chat",
                123,
                snapshot,
            )

        markup = edit.call_args.kwargs["reply_markup"]
        self.assertEqual([], markup.keyboard)

    def _assert_refresh_waits_for_manual_operation(
        self,
        operation,
        operation_entered,
        release_operation,
    ):
        refresh_entered = threading.Event()
        operation_result = []

        def blocking_operation():
            operation_result.append(operation())

        def record_refresh(label="daily"):
            refresh_entered.set()

        with patch.object(
            app_daily_market_scheduler,
            "_daily_market_data_refresh",
            side_effect=record_refresh,
        ):
            operation_thread = threading.Thread(target=blocking_operation)
            refresh_thread = threading.Thread(
                target=app_daily_market_scheduler.daily_market_data_refresh
            )
            operation_thread.start()
            self.assertTrue(operation_entered.wait(timeout=1.0))
            refresh_thread.start()
            self.assertFalse(refresh_entered.wait(timeout=0.05))
            release_operation.set()
            operation_thread.join(timeout=1.0)
            refresh_thread.join(timeout=1.0)

        self.assertFalse(operation_thread.is_alive())
        self.assertFalse(refresh_thread.is_alive())
        self.assertTrue(refresh_entered.is_set())
        return operation_result

    def test_manual_scan_uses_scheduler_operation_lock(self):
        operation_entered = threading.Event()
        release_operation = threading.Event()

        def blocking_scan(*args, **kwargs):
            operation_entered.set()
            release_operation.wait(timeout=1.0)
            return "report", "report-id"

        with patch.object(
            RUN_telegram_bot_server,
            "run_compiled_scan",
            side_effect=blocking_scan,
        ):
            results = self._assert_refresh_waits_for_manual_operation(
                lambda: RUN_telegram_bot_server.run_manual_scan("123", lambda _: None),
                operation_entered,
                release_operation,
            )

        self.assertEqual([("report", "report-id")], results)

    def test_quick_report_batch_uses_scheduler_operation_lock(self):
        operation_entered = threading.Event()
        release_operation = threading.Event()

        def blocking_quick_report(ticker):
            if ticker == "AAA":
                operation_entered.set()
                release_operation.wait(timeout=1.0)
            return f"report:{ticker}"

        with patch.object(
            RUN_telegram_bot_server,
            "quick_report",
            side_effect=blocking_quick_report,
        ):
            results = self._assert_refresh_waits_for_manual_operation(
                lambda: RUN_telegram_bot_server.build_quick_reports(["AAA", "BBB"]),
                operation_entered,
                release_operation,
            )

        self.assertEqual(
            [["**[1/2]**\nreport:AAA", "**[2/2]**\nreport:BBB"]],
            results,
        )


if __name__ == "__main__":
    unittest.main()
