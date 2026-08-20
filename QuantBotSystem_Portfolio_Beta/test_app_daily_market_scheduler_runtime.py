import builtins
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

import app_daily_market_scheduler
import app_runtime_lock


class SchedulerRuntimeSafetyTests(unittest.TestCase):
    def test_scheduled_scan_uses_selected_fixed_exit_policy(self):
        session = {
            "selected_groups": {"CSQ_CP"},
            "strategy": "MA_CROSS",
            "exit_policy": "FIXED_5D",
            "neutralization": "COUNTRY",
            "position_mode": "LONG_SHORT_NEUTRAL",
            "custom_tickers": [],
            "scheduler_active": True,
        }

        with patch.object(
            app_daily_market_scheduler.runtime_settings,
            "get_setting",
            return_value=["chat-id"],
        ), patch.dict(
            app_daily_market_scheduler.USER_SESSIONS,
            {"chat-id": session},
            clear=True,
        ), patch.object(
            app_daily_market_scheduler,
            "compile_selected_universe",
            return_value=["AAA"],
        ), patch.object(
            app_daily_market_scheduler.strategy_ma_cross,
            "scan_ma_cross",
            return_value="report",
        ) as scan, patch.object(
            app_daily_market_scheduler,
            "send_split_telegram_message",
        ):
            app_daily_market_scheduler._daily_market_scanner()

        self.assertEqual(5, scan.call_args.kwargs["fixed_exit_sessions"])
        self.assertEqual("COUNTRY", scan.call_args.kwargs["neutralization"])
        self.assertEqual(
            "LONG_SHORT_NEUTRAL",
            scan.call_args.kwargs["position_mode"],
        )

    def test_scheduler_send_error_fully_redacts_bot_token(self):
        token = "123456:ABC_def-secret"
        encoded_token = quote(token, safe="")
        error = RuntimeError(
            "POST failed for "
            f"https://api.telegram.org/bot{token}/sendMessage "
            f"(encoded={encoded_token})"
        )

        with patch.object(
            app_daily_market_scheduler.runtime_settings,
            "get_setting",
            return_value=token,
        ), patch.object(
            app_daily_market_scheduler.requests,
            "post",
            side_effect=error,
        ), patch.object(builtins, "print") as printed:
            app_daily_market_scheduler.send_split_telegram_message(
                "chat-id",
                "message",
            )

        output = "\n".join(
            " ".join(str(value) for value in call.args)
            for call in printed.call_args_list
        )
        self.assertNotIn(token, output)
        self.assertNotIn(encoded_token, output)
        self.assertIn("[REDACTED_BOT_TOKEN]", output)

    def test_scheduled_scan_and_refresh_share_one_operation_lock(self):
        scan_entered = threading.Event()
        release_scan = threading.Event()
        refresh_entered = threading.Event()

        def blocking_scan():
            scan_entered.set()
            release_scan.wait(timeout=1.0)

        def record_refresh(label="daily"):
            refresh_entered.set()

        with patch.object(
            app_daily_market_scheduler,
            "_daily_market_scanner",
            side_effect=blocking_scan,
        ), patch.object(
            app_daily_market_scheduler,
            "_daily_market_data_refresh",
            side_effect=record_refresh,
        ):
            scan_thread = threading.Thread(
                target=app_daily_market_scheduler.daily_market_scanner
            )
            refresh_thread = threading.Thread(
                target=app_daily_market_scheduler.daily_market_data_refresh
            )
            scan_thread.start()
            self.assertTrue(scan_entered.wait(timeout=1.0))
            refresh_thread.start()
            self.assertFalse(refresh_entered.wait(timeout=0.05))
            release_scan.set()
            scan_thread.join(timeout=1.0)
            refresh_thread.join(timeout=1.0)

        self.assertFalse(scan_thread.is_alive())
        self.assertFalse(refresh_thread.is_alive())
        self.assertTrue(refresh_entered.is_set())

    def test_runtime_operation_lock_is_reentrant(self):
        with app_runtime_lock.serialized_runtime_operation():
            with app_runtime_lock.serialized_runtime_operation():
                pass


if __name__ == "__main__":
    unittest.main()
