import threading
import unittest
from unittest.mock import patch

import RUN_telegram_bot_server
import app_daily_market_scheduler


class TelegramServerRuntimeSafetyTests(unittest.TestCase):
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
