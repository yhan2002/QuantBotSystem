import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import RUN_telegram_bot_server
import app_daily_market_scheduler


class TelegramServerRuntimeSafetyTests(unittest.TestCase):
    def test_see_detail_sends_text_without_downloading_export(self):
        call = SimpleNamespace(
            id="callback-id",
            data="scan_detail:report123",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123),
                message_id=456,
            ),
        )
        payload = {
            "detail": "detail text",
            "export_path": None,
            "export_spec": {"version": 1},
        }

        with patch.object(
            RUN_telegram_bot_server,
            "check_permission",
            return_value=True,
        ), patch.object(
            RUN_telegram_bot_server,
            "get_or_create_session",
            return_value={},
        ), patch.object(
            RUN_telegram_bot_server,
            "get_backtest_report",
            return_value=payload,
        ), patch.object(
            RUN_telegram_bot_server,
            "send_long_message",
        ) as send_text, patch.object(
            RUN_telegram_bot_server.bot,
            "answer_callback_query",
        ) as answer, patch.object(
            RUN_telegram_bot_server.bot,
            "send_document",
        ) as send_document:
            RUN_telegram_bot_server.handle_menu_callbacks(call)

        answer.assert_called_once_with(
            "callback-id",
            "Sending backtest detail.",
        )
        send_text.assert_called_once()
        self.assertEqual("detail text", send_text.call_args.args[1])
        send_document.assert_not_called()

    def test_export_only_payload_does_not_send_empty_detail(self):
        call = SimpleNamespace(
            id="callback-id",
            data="scan_detail:report123",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123),
                message_id=456,
            ),
        )
        payload = {
            "detail": None,
            "export_path": None,
            "export_spec": {"version": 1},
        }

        with patch.object(
            RUN_telegram_bot_server,
            "check_permission",
            return_value=True,
        ), patch.object(
            RUN_telegram_bot_server,
            "get_or_create_session",
            return_value={},
        ), patch.object(
            RUN_telegram_bot_server,
            "get_backtest_report",
            return_value=payload,
        ), patch.object(
            RUN_telegram_bot_server,
            "send_long_message",
        ) as send_text, patch.object(
            RUN_telegram_bot_server.bot,
            "answer_callback_query",
        ) as answer:
            RUN_telegram_bot_server.handle_menu_callbacks(call)

        send_text.assert_not_called()
        answer.assert_called_once_with(
            "callback-id",
            "This detail has expired. Run the scan again.",
            show_alert=True,
        )

    def test_explicit_export_callback_is_submitted_to_background_worker(self):
        call = SimpleNamespace(
            id="callback-id",
            data="scan_export:report123",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123),
                message_id=456,
            ),
        )
        payload = {
            "detail": "detail",
            "export_path": None,
            "export_spec": {"version": 1},
        }

        with patch.object(
            RUN_telegram_bot_server,
            "check_permission",
            return_value=True,
        ), patch.object(
            RUN_telegram_bot_server,
            "get_or_create_session",
            return_value={},
        ), patch.object(
            RUN_telegram_bot_server,
            "get_backtest_report",
            return_value=payload,
        ), patch.object(
            RUN_telegram_bot_server.bot,
            "send_message",
            return_value=SimpleNamespace(message_id=789),
        ), patch.object(
            RUN_telegram_bot_server.bot,
            "answer_callback_query",
        ) as answer, patch.object(
            RUN_telegram_bot_server,
            "submit_background_operation",
            return_value=SimpleNamespace(created=True),
        ) as submit, patch.object(
            RUN_telegram_bot_server,
            "prepare_backtest_export",
        ) as prepare:
            RUN_telegram_bot_server.handle_menu_callbacks(call)

        submit.assert_called_once()
        prepare.assert_not_called()
        answer.assert_called_once_with(
            "callback-id",
            "Preparing backtest export.",
        )

    def test_explicit_export_uses_and_removes_temporary_directory(self):
        captured = {}

        def materialize(_spec, directory):
            captured["directory"] = directory
            path = Path(directory) / "test_bundle.zip"
            path.write_bytes(b"zip")
            return path

        with patch.object(
            RUN_telegram_bot_server.core_universe_backtest,
            "materialize_backtest_export",
            side_effect=materialize,
        ) as materializer, patch.object(
            RUN_telegram_bot_server.bot,
            "send_document",
        ) as send_document:
            RUN_telegram_bot_server.send_backtest_export(
                "123",
                {"export_spec": {"version": 1}},
            )

        materializer.assert_called_once()
        send_document.assert_called_once()
        self.assertFalse(Path(captured["directory"]).exists())

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

    def test_benchmark_stage_uses_shared_range_without_ticker_count(self):
        emitted = []
        adapter = RUN_telegram_bot_server._benchmark_progress_adapter(
            lambda *args: emitted.append(args),
            threading.Event(),
        )

        adapter(2, 4, "CSV 검증")

        self.assertEqual([(50.0, "CSV 검증")], emitted)

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
        benchmark_adapter = RUN_telegram_bot_server._benchmark_progress_adapter(
            lambda *args: None,
            cancelled,
        )

        with self.assertRaises(
            RUN_telegram_bot_server.app_scan_jobs.JobCancelled
        ):
            scan_adapter("CURRENT_SCAN", 1, 2, "AAA", True)
        with self.assertRaises(
            RUN_telegram_bot_server.app_scan_jobs.JobCancelled
        ):
            download_adapter(1, 2, "AAA")
        with self.assertRaises(
            RUN_telegram_bot_server.app_scan_jobs.JobCancelled
        ):
            benchmark_adapter(1, 2, "CSV 읽기")

    def test_benchmark_refresh_runs_under_runtime_lock_and_formats_result(self):
        summary = {
            "ticker": "ACWI",
            "updated": 2,
            "first_date": "2026-01-01",
            "latest_date": "2026-07-27",
            "source_kind": "yfinance",
            "data_basis": "Adj Close",
            "cached_rows": 2,
            "cached_first_date": "2026-01-01",
            "cached_latest_date": "2026-07-27",
            "db_path": "market_data.sqlite",
        }
        captured = {}

        def capture_submission(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(created=True, progress=1)

        with patch.object(
            RUN_telegram_bot_server,
            "submit_background_operation",
            side_effect=capture_submission,
        ), patch.object(
            RUN_telegram_bot_server,
            "serialized_runtime_call",
            return_value=summary,
        ) as serialized, patch.object(
            RUN_telegram_bot_server.bot,
            "edit_message_text",
        ) as edit:
            submission = (
                RUN_telegram_bot_server.submit_benchmark_background_operation(
                    "chat",
                    123,
                )
            )
            progress = []
            result = captured["runner"](
                lambda *args: progress.append(args),
                threading.Event(),
            )
            captured["on_result"](result)

        self.assertTrue(submission.created)
        self.assertEqual(summary, result)
        self.assertEqual((5, "ACWI 다운로드 준비"), progress[0])
        serialized.assert_called_once()
        self.assertIs(
            serialized.call_args.args[0],
            RUN_telegram_bot_server.core_market_data_store.refresh_market_benchmark,
        )
        self.assertEqual("BENCHMARK_REFRESH", captured["task_type"])
        self.assertIn("ACWI", edit.call_args.args[0])
        self.assertNotIn("parse_mode", edit.call_args.kwargs)

    def test_admin_benchmark_callback_routes_to_background_submission(self):
        call = SimpleNamespace(
            id="callback-id",
            data="admin_refresh_benchmark",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123),
                message_id=456,
            ),
        )
        submission = SimpleNamespace(created=True, progress=1)

        with patch.object(
            RUN_telegram_bot_server,
            "check_permission",
            return_value=True,
        ), patch.object(
            RUN_telegram_bot_server,
            "submit_benchmark_background_operation",
            return_value=submission,
        ) as submit, patch.object(
            RUN_telegram_bot_server.bot,
            "answer_callback_query",
        ) as answer:
            RUN_telegram_bot_server.handle_menu_callbacks(call)

        submit.assert_called_once_with("123", 456)
        answer.assert_called_once_with(
            "callback-id",
            "Background benchmark refresh started.",
            show_alert=False,
        )

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
