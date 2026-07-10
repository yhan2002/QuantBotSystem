# File name: daily_market_scheduler.py
# Sends scheduled Telegram reports using the selected universe and strategy.
import time
import threading
from datetime import datetime

import requests
import schedule

import box_range_engine
import config
import ma_cross_engine
import market_data_store
import value_reversion_engine
from bot_session import USER_SESSIONS, compile_selected_universe

_scheduler_started = False
MORNING_DATA_REFRESH_TIME = "08:00"
EVENING_DATA_REFRESH_TIME = "19:00"
SCHEDULED_SCAN_TIME = "19:10"


def send_split_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    max_length = 4000

    chunks = [text[i : i + max_length] for i in range(0, len(text), max_length)]
    for chunk in chunks:
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=10)
            time.sleep(0.5)
        except Exception as e:
            print(f"[scheduler send failed] Chat ID {chat_id}: {e}")


def daily_market_scanner():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Scheduled market scanner started.")

    for chat_id in config.ALLOWED_CHAT_IDS:
        session = USER_SESSIONS.get(
            chat_id,
            {
                "selected_groups": set(["CSQ_CP"]),
                "strategy": "MA_CROSS",
                "custom_tickers": [],
                "scheduler_active": True,
            },
        )

        if not session.get("scheduler_active", True):
            print(f"[{now_str}] Chat ID {chat_id}: scheduler is OFF.")
            continue

        selected_strategy = session.get("strategy", "MA_CROSS")
        compiled_universe = compile_selected_universe(session)
        if not compiled_universe:
            compiled_universe = config.universe

        try:
            if selected_strategy == "MA_CROSS":
                report_content = ma_cross_engine.scan_ma_cross(compiled_universe)
                header = f"*Scheduled Alert* MA Momentum Scan ({now_str})\n"
            elif selected_strategy == "BOX_RANGE":
                report_content = box_range_engine.scan_box_range(compiled_universe)
                header = f"*Scheduled Alert* Box Range Scan ({now_str})\n"
            elif selected_strategy == "VALUE_REVERSION":
                report_content = value_reversion_engine.scan_value_reversion(
                    compiled_universe
                )
                header = f"*Scheduled Alert* Value Reversion Scan ({now_str})\n"
            else:
                report_content = f"Unknown strategy: {selected_strategy}"
                header = f"*Scheduled Alert* ({now_str})\n"

            if chat_id == "-5489984462":
                for marker in ["Backtest Summary"]:
                    if marker in report_content:
                        report_content = report_content.split(marker)[0].strip()
                        report_content += (
                            "\n\n*Backtest detail is available in private/admin sessions.*"
                        )
                        break

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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Market data refresh started. label={label}")
    try:
        summary = market_data_store.preload_all_universe(config)
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
