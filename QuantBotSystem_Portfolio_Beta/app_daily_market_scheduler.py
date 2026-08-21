# File name: daily_market_scheduler.py
# Sends scheduled Telegram reports using the selected universe and strategy.
import time
import threading
from datetime import datetime
from urllib.parse import quote

import requests
import schedule

import app_runtime_lock
import strategy_box_range
import config
import runtime_settings
import strategy_ma_cross
import core_market_data_store
import strategy_price_liquidity_reversion
import core_universe_backtest
from app_bot_session import (
    BOX_HYBRID_EXIT_POLICY,
    BOX_HYBRID_EXIT_SESSIONS,
    EXIT_POLICY_SESSIONS,
    STRATEGY_LABELS,
    USER_SESSIONS,
    compile_selected_universe,
)

_scheduler_started = False
MORNING_DATA_REFRESH_TIME = "08:00"
EVENING_DATA_REFRESH_TIME = "19:00"
SCHEDULED_SCAN_TIME = "19:10"


def _redact_bot_token(message, bot_token):
    redacted = str(message)
    secret = str(bot_token or "")
    if not secret:
        return redacted
    variants = {secret, quote(secret, safe="")}
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            redacted = redacted.replace(variant, "[REDACTED_BOT_TOKEN]")
    return redacted


def send_split_telegram_message(chat_id, text):
    bot_token = runtime_settings.get_setting("BOT_TOKEN", config.BOT_TOKEN)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    max_length = 4000

    chunks = [text[i : i + max_length] for i in range(0, len(text), max_length)]
    for chunk in chunks:
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
            time.sleep(0.5)
        except Exception as e:
            safe_error = _redact_bot_token(e, bot_token)
            print(f"[scheduler send failed] Chat ID {chat_id}: {safe_error}")


def daily_market_scanner():
    with app_runtime_lock.serialized_runtime_operation():
        return _daily_market_scanner()


def _daily_market_scanner():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Scheduled market scanner started.")

    for chat_id in runtime_settings.get_setting(
        "ALLOWED_CHAT_IDS", config.ALLOWED_CHAT_IDS
    ):
        session = USER_SESSIONS.get(
            chat_id,
            {
                "selected_groups": set(["CSQ_CP"]),
                "strategy": "MA_CROSS",
                "exit_policy": "NATIVE",
                "neutralization": "NONE",
                "position_mode": "LONG_SHORT_NEUTRAL",
                "custom_tickers": [],
                "scheduler_active": True,
            },
        )

        if not session.get("scheduler_active", True):
            print(f"[{now_str}] Chat ID {chat_id}: scheduler is OFF.")
            continue

        selected_strategy = session.get("strategy", "MA_CROSS")
        if selected_strategy not in STRATEGY_LABELS:
            selected_strategy = "MA_CROSS"
            session["strategy"] = selected_strategy
        neutralization = session.get("neutralization", "NONE")
        position_mode = session.get("position_mode", "LONG_SHORT_NEUTRAL")
        fixed_exit_sessions = EXIT_POLICY_SESSIONS.get(
            session.get("exit_policy", "NATIVE")
        )
        hybrid_exit_sessions = (
            BOX_HYBRID_EXIT_SESSIONS
            if selected_strategy == "BOX_RANGE"
            and session.get("exit_policy") == BOX_HYBRID_EXIT_POLICY
            else None
        )
        compiled_universe = compile_selected_universe(session)
        if not compiled_universe:
            compiled_universe = config.universe

        try:
            if selected_strategy == "MA_CROSS":
                report_content = strategy_ma_cross.scan_ma_cross(
                    compiled_universe,
                    fixed_exit_sessions=fixed_exit_sessions,
                    neutralization=neutralization,
                    position_mode=position_mode,
                )
                header = f"*Scheduled Alert* Momentum Scan ({now_str})\n"
            elif selected_strategy == "BOX_RANGE":
                report_content = strategy_box_range.scan_box_range(
                    compiled_universe,
                    fixed_exit_sessions=fixed_exit_sessions,
                    hybrid_exit_sessions=hybrid_exit_sessions,
                    neutralization=neutralization,
                    position_mode=position_mode,
                )
                header = f"*Scheduled Alert* Box Range Scan ({now_str})\n"
            elif selected_strategy == "VALUE_REVERSION":
                report_content = strategy_price_liquidity_reversion.scan_value_reversion(
                    compiled_universe,
                    fixed_exit_sessions=fixed_exit_sessions,
                    neutralization=neutralization,
                    position_mode=position_mode,
                )
                header = f"*Scheduled Alert* Reversion Scan ({now_str})\n"
            else:
                report_content = f"Unknown strategy: {selected_strategy}"
                header = f"*Scheduled Alert* ({now_str})\n"

            report_content, _ = core_universe_backtest.split_summary_detail(report_content)

            final_msg = f"{header}\n{report_content}"
            send_split_telegram_message(chat_id, final_msg)
            print(
                f"[{now_str}] Chat ID {chat_id}: scheduled report sent "
                f"({len(compiled_universe)} tickers)."
            )

        except Exception as e:
            error_msg = f"[scheduler error] Chat ID {chat_id}: {e}"
            print(error_msg)
            send_split_telegram_message(chat_id, error_msg)


def daily_market_data_refresh(label="daily"):
    with app_runtime_lock.serialized_runtime_operation():
        return _daily_market_data_refresh(label=label)


def _daily_market_data_refresh(label="daily"):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Market data refresh started. label={label}")
    try:
        summary = core_market_data_store.preload_all_universe(config)
        print(
            f"[{now_str}] Market data refresh complete. label={label}, "
            f"tickers={summary['requested']}, rows_updated={summary['updated']}, "
            f"failed={len(summary['failed'])}"
        )
        if summary["failed"]:
            print(f"[{now_str}] Data refresh failed tickers: {', '.join(summary['failed'])}")
    except Exception as e:
        print(f"[{now_str}] Daily market data refresh failed: {e}")


def configure_jobs():
    schedule.clear("market_scanner")
    schedule.clear("market_data_refresh")
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at(SCHEDULED_SCAN_TIME).do(daily_market_scanner).tag(
            "market_scanner"
        )
    schedule.every().day.at(MORNING_DATA_REFRESH_TIME).do(
        daily_market_data_refresh, label="morning_us_eod"
    ).tag("market_data_refresh")
    schedule.every().day.at(EVENING_DATA_REFRESH_TIME).do(
        daily_market_data_refresh, label="evening_asia_eod"
    ).tag("market_data_refresh")


def run_scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(60)


def start_scheduler_thread():
    global _scheduler_started
    if _scheduler_started:
        return

    configure_jobs()
    thread = threading.Thread(target=run_scheduler_loop, daemon=True)
    thread.start()
    _scheduler_started = True
    print("[scheduled market scanner] started in background thread.")


if __name__ == "__main__":
    configure_jobs()
    print(
        "[scheduled market scanner] waiting for jobs: "
        f"data refresh {MORNING_DATA_REFRESH_TIME}/{EVENING_DATA_REFRESH_TIME}, "
        f"scan {SCHEDULED_SCAN_TIME}."
    )
    while True:
        schedule.run_pending()
        time.sleep(60)
