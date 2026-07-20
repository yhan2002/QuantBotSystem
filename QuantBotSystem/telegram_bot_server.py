"""Backward-compatible launcher for the canonical bot entry point."""

import app_daily_market_scheduler
from RUN_telegram_bot_server import bot


if __name__ == "__main__":
    print("[DEBUG] Quant Bot server started.")
    app_daily_market_scheduler.start_scheduler_thread()
    bot.infinity_polling()
