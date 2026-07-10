# File name: telegram_bot_server.py
# Routes Telegram button events and user text input to feature modules.
import os

import telebot
from telebot.apihelper import ApiTelegramException

import config
import daily_market_scheduler
import market_data_store
from bot_reports import boxbt_report, quick_report, run_compiled_scan
from bot_session import (
    CUSTOM_GROUP_CODE,
    STRATEGY_LABELS,
    check_permission,
    clear_direct_input,
    compile_selected_universe,
    get_or_create_session,
    parse_tickers,
    set_direct_input,
    sync_config_file,
)
from bot_ui import (
    admin_text,
    build_back_home_markup,
    build_admin_markup,
    build_home_markup,
    build_settings_markup,
    build_strategy_markup,
    build_universe_markup,
    home_text,
    settings_text,
)


bot = telebot.TeleBot(config.BOT_TOKEN)


def permission_message(chat_id):
    return (
        "This chat is not allowed for this test bot.\n"
        f"chat_id: `{chat_id}`\n\n"
        "Add this chat_id to ALLOWED_CHAT_IDS in catalyst_test_full/config.py, "
        "then restart the test bot."
    )


def send_long_message(chat_id, text, first_message_id=None, markup=None):
    max_length = 4000
    if len(text) <= max_length and first_message_id:
        bot.edit_message_text(
            text,
            chat_id,
            first_message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    if first_message_id:
        bot.edit_message_text("Scan complete. Sending report...", chat_id, first_message_id)

    chunks = [text[i : i + max_length] for i in range(0, len(text), max_length)]
    for idx, chunk in enumerate(chunks):
        chunk_markup = markup if idx == len(chunks) - 1 else None
        bot.send_message(chat_id, chunk, parse_mode="Markdown", reply_markup=chunk_markup)


@bot.message_handler(commands=["menu", "start"])
def handle_menu(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        bot.reply_to(message, permission_message(chat_id), parse_mode="Markdown")
        return
    session = get_or_create_session(chat_id)
    session["awaiting"] = None
    bot.send_message(
        chat_id,
        home_text(chat_id),
        reply_markup=build_home_markup(),
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_menu_callbacks(call):
    chat_id = str(call.message.chat.id)
    if not check_permission(chat_id):
        bot.answer_callback_query(call.id, "This chat is not allowed.", show_alert=True)
        return

    session = get_or_create_session(chat_id)
    data = call.data

    try:
        if data == "go_home":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                home_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_home_markup(),
            )

        elif data == "settings":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                settings_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_settings_markup(chat_id),
            )

        elif data == "admin":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                admin_text(),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "settings_universe":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Universe Settings**\n\nSelect one or more groups.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_universe_markup(chat_id),
            )

        elif data == "settings_strategy":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Strategy Settings**\n\nChoose the scan engine.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_strategy_markup(chat_id),
            )

        elif data.startswith("toggle_group:"):
            group_code = data.split(":", 1)[1]
            if group_code == CUSTOM_GROUP_CODE and not session.get("custom_tickers"):
                bot.answer_callback_query(
                    call.id, "Set Direct Input first.", show_alert=True
                )
                return

            if group_code in session["selected_groups"]:
                if len(session["selected_groups"]) > 1:
                    session["selected_groups"].remove(group_code)
                    bot.answer_callback_query(call.id, "Removed.")
                else:
                    bot.answer_callback_query(
                        call.id,
                        "At least one universe must stay selected.",
                        show_alert=True,
                    )
                    return
            else:
                session["selected_groups"].add(group_code)
                bot.answer_callback_query(call.id, "Added.")

            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_universe_markup(chat_id),
            )

        elif data == "await_custom_universe":
            session["awaiting"] = "custom_universe"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send the tickers to use as Direct Input.\n"
                "Examples: `TSLA NVDA`, `1099 HK, 9983 JP, 600276 SH`",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )

        elif data == "clear_custom":
            clear_direct_input(session)
            bot.answer_callback_query(call.id, "Direct Input cleared.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_universe_markup(chat_id),
            )

        elif data.startswith("set_strategy:"):
            strategy = data.split(":", 1)[1]
            if strategy in STRATEGY_LABELS:
                session["strategy"] = strategy
            bot.answer_callback_query(call.id, "Strategy updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_strategy_markup(chat_id),
            )

        elif data == "toggle_scheduler":
            session["scheduler_active"] = not session.get("scheduler_active", True)
            bot.answer_callback_query(
                call.id,
                "Scheduler ON." if session["scheduler_active"] else "Scheduler OFF.",
            )
            bot.edit_message_text(
                settings_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_settings_markup(chat_id),
            )

        elif data == "run_scan":
            bot.answer_callback_query(call.id, "Starting scan.")
            report = run_compiled_scan(
                chat_id,
                progress_callback=lambda msg: bot.edit_message_text(
                    msg, chat_id, call.message.message_id
                ),
            )
            send_long_message(
                chat_id,
                report,
                first_message_id=call.message.message_id,
                markup=build_back_home_markup(),
            )

        elif data == "admin_preload_all":
            bot.answer_callback_query(call.id, "Starting full market data preload.")
            bot.edit_message_text(
                "Preloading all market data. This can take several minutes...",
                chat_id,
                call.message.message_id,
            )
            summary = market_data_store.preload_all_universe(config)
            report = market_data_store.format_cache_summary(
                summary, title="Full Market Data Preload Complete"
            )
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_refresh_selected":
            selected = compile_selected_universe(session)
            if not selected:
                bot.answer_callback_query(call.id, "No selected tickers.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "Refreshing selected universe.")
            bot.edit_message_text(
                f"Refreshing selected universe ({len(selected)} tickers)...",
                chat_id,
                call.message.message_id,
            )
            summary = market_data_store.ensure_price_data(selected, min_period="1y")
            report = market_data_store.format_cache_summary(
                summary, title="Selected Universe Refresh Complete"
            )
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_cache_status":
            all_tickers = sorted(set(config.universe))
            report = (
                "**Data Cache Status**\n\n"
                f"Configured tickers: {len(all_tickers)}\n"
                f"{market_data_store.freshness_summary(all_tickers)}\n"
                f"DB path: `{market_data_store.DB_PATH}`"
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "await_quick":
            session["awaiting"] = "quick"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send one ticker for a Quick Check.\nExample: `TSLA`",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )

        elif data == "await_boxbt":
            session["awaiting"] = "boxbt"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send one ticker for Box TP/SL Test.\nExample: `TSLA`",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )

    except ApiTelegramException as e:
        if "message is not modified" not in str(e.description):
            raise e
    except Exception as e:
        bot.answer_callback_query(call.id, "Error.")
        bot.edit_message_text(
            f"Error: {e}",
            chat_id,
            call.message.message_id,
            reply_markup=build_back_home_markup(),
        )


@bot.message_handler(commands=["whoami", "chatid"])
def handle_whoami(message):
    chat_id = str(message.chat.id)
    bot.reply_to(message, f"chat_id: `{chat_id}`", parse_mode="Markdown")


@bot.message_handler(commands=["custom"])
def handle_custom_universe_legacy(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    tickers = parse_tickers(message.text.partition(" ")[2])
    if not tickers:
        bot.reply_to(message, "Open /menu -> Settings -> Universe -> Set Direct Input.")
        return
    session = get_or_create_session(chat_id)
    set_direct_input(session, tickers)
    bot.reply_to(
        message,
        f"Direct Input set ({len(tickers)}): `{', '.join(tickers)}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["boxbt"])
def handle_box_backtest_legacy(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    tickers = parse_tickers(message.text.partition(" ")[2])
    if not tickers:
        bot.reply_to(message, "Open /menu -> Box TP/SL Test.")
        return
    bot.reply_to(message, boxbt_report(tickers[0]), parse_mode="Markdown")


@bot.message_handler(commands=["list"])
def handle_list(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    ticker_list = ", ".join(sorted(config.universe))
    bot.reply_to(message, f"Master universe ({len(config.universe)} tickers):\n{ticker_list}")


@bot.message_handler(commands=["add"])
def handle_add_ticker(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    try:
        payload = message.text.partition(" ")[2]
        tickers = parse_tickers(payload)
        if not tickers:
            bot.reply_to(
                message,
                "Usage: `/add TSLA` or `/add 1099 HK` or `/add 9983 JP`",
                parse_mode="Markdown",
            )
            return
        new_ticker = tickers[0]
        if new_ticker in config.universe:
            bot.reply_to(message, f"{new_ticker} already exists.")
            return
        config.universe.append(new_ticker)
        if sync_config_file(os.path.dirname(os.path.abspath(__file__))):
            bot.reply_to(message, f"{new_ticker} added. Total: {len(config.universe)}")
        else:
            bot.reply_to(message, "File sync failed.")
    except Exception as e:
        bot.reply_to(message, f"Add failed: {e}")


@bot.message_handler(commands=["del", "delete"])
def handle_del_ticker(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    try:
        payload = message.text.partition(" ")[2]
        tickers = parse_tickers(payload)
        if not tickers:
            bot.reply_to(
                message,
                "Usage: `/del AAPL` or `/del 1099 HK` or `/del 9983 JP`",
                parse_mode="Markdown",
            )
            return
        target_ticker = tickers[0]
        if target_ticker not in config.universe:
            bot.reply_to(message, f"{target_ticker} is not in the master universe.")
            return
        config.universe.remove(target_ticker)
        if sync_config_file(os.path.dirname(os.path.abspath(__file__))):
            bot.reply_to(message, f"{target_ticker} removed. Total: {len(config.universe)}")
        else:
            bot.reply_to(message, "File sync failed.")
    except Exception as e:
        bot.reply_to(message, f"Delete failed: {e}")


@bot.message_handler(content_types=["text"])
def handle_pending_text(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return

    session = get_or_create_session(chat_id)
    awaiting = session.get("awaiting")
    if not awaiting:
        bot.reply_to(message, "Use /menu to start.")
        return

    tickers = parse_tickers(message.text)
    if not tickers:
        bot.reply_to(message, "No ticker detected. Try again or open /menu.")
        return

    try:
        if awaiting == "custom_universe":
            set_direct_input(session, tickers)
            bot.reply_to(
                message,
                f"Direct Input set ({len(tickers)}): `{', '.join(tickers)}`",
                parse_mode="Markdown",
                reply_markup=build_home_markup(),
            )
        elif awaiting == "quick":
            session["awaiting"] = None
            bot.reply_to(message, f"Running Quick Check for {tickers[0]}...")
            bot.send_message(
                chat_id,
                quick_report(tickers[0]),
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )
        elif awaiting == "boxbt":
            session["awaiting"] = None
            bot.reply_to(message, f"Running Box TP/SL Test for {tickers[0]}...")
            bot.send_message(
                chat_id,
                boxbt_report(tickers[0]),
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )
    except Exception as e:
        session["awaiting"] = None
        bot.reply_to(message, f"Error: {e}", reply_markup=build_back_home_markup())


if __name__ == "__main__":
    print("[DEBUG] Quant Bot server started.")
    daily_market_scheduler.start_scheduler_thread()
    bot.infinity_polling()
