import builtins
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

import app_daily_market_scheduler
import app_runtime_lock


class SchedulerRuntimeSafetyTests(unittest.TestCase):
    def test_morning_refresh_updates_acwi_but_evening_refresh_does_not(self):
        summary = {"requested": 1, "updated": 1, "failed": []}
        with patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "preload_all_universe",
            return_value=summary,
        ), patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "refresh_market_benchmark",
            return_value={"latest_date": "2026-09-16"},
        ) as refresh_benchmark:
            app_daily_market_scheduler._daily_market_data_refresh(
                label="morning_us_eod"
            )
            app_daily_market_scheduler._daily_market_data_refresh(
                label="evening_asia_eod"
            )

        refresh_benchmark.assert_called_once_with()

    def test_morning_acwi_refresh_survives_universe_refresh_failure(self):
        with patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "preload_all_universe",
            side_effect=RuntimeError("universe failed"),
        ), patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "refresh_market_benchmark",
            return_value={"latest_date": "2026-09-16"},
        ) as refresh_benchmark, patch.object(builtins, "print") as printed:
            app_daily_market_scheduler._daily_market_data_refresh(
                label="morning_us_eod"
            )

        refresh_benchmark.assert_called_once_with()
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("Universe market data refresh failed", output)
        self.assertIn("ACWI refresh complete", output)

    def test_acwi_failure_keeps_successful_universe_refresh_result(self):
        summary = {"requested": 1, "updated": 1, "failed": []}
        with patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "preload_all_universe",
            return_value=summary,
        ), patch.object(
            app_daily_market_scheduler.core_market_data_store,
            "refresh_market_benchmark",
            side_effect=RuntimeError("ACWI failed"),
        ), patch.object(builtins, "print") as printed:
            app_daily_market_scheduler._daily_market_data_refresh(
                label="morning_us_eod"
            )

        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("Market data refresh complete", output)
        self.assertIn("ACWI benchmark refresh failed", output)

    def test_scheduled_relative_equilibrium_scan_uses_common_settings(self):
        session = {
            "selected_groups": {"CSQ_CP"},
            "strategy": "RELATIVE_EQUILIBRIUM",
            "exit_policy": "FIXED_5D",
            "neutralization": "SECTOR",
            "position_mode": "LONG_ONLY",
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
            app_daily_market_scheduler.strategy_relative_equilibrium,
            "scan_relative_equilibrium",
            return_value="report",
        ) as scan, patch.object(
            app_daily_market_scheduler,
            "send_split_telegram_message",
        ) as send:
            app_daily_market_scheduler._daily_market_scanner()

        self.assertEqual(["AAA"], scan.call_args.args[0])
        self.assertEqual(5, scan.call_args.kwargs["fixed_exit_sessions"])
        self.assertEqual("SECTOR", scan.call_args.kwargs["neutralization"])
        self.assertEqual("LONG_ONLY", scan.call_args.kwargs["position_mode"])
        self.assertIn("Relative Equilibrium Scan", send.call_args.args[1])

    def test_scheduled_box_scan_uses_hybrid_exit_policy(self):
        session = {
            "selected_groups": {"CSQ_CP"},
            "strategy": "BOX_RANGE",
            "exit_policy": "BOX_HYBRID_5D",
            "neutralization": "NONE",
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
            app_daily_market_scheduler.strategy_box_range,
            "scan_box_range",
            return_value="report",
        ) as scan, patch.object(
            app_daily_market_scheduler,
            "send_split_telegram_message",
        ):
            app_daily_market_scheduler._daily_market_scanner()
            hybrid_kwargs = scan.call_args.kwargs
            session["exit_policy"] = "BOX_BREAKOUT_ENTRY"
            app_daily_market_scheduler._daily_market_scanner()
            breakout_kwargs = scan.call_args.kwargs

        self.assertIsNone(hybrid_kwargs["fixed_exit_sessions"])
        self.assertEqual(5, hybrid_kwargs["hybrid_exit_sessions"])
        self.assertEqual("BOX_BREAKOUT", breakout_kwargs["entry_execution"])
        self.assertIsNone(breakout_kwargs["hybrid_exit_sessions"])

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
        self.assertNotIn("include_ema_alignment_events", scan.call_args.kwargs)

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
