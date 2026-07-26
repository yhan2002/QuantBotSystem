# File name: telegram_bot_server.py
# Routes Telegram button events and user text input to feature modules.
import os

import telebot
from telebot.apihelper import ApiTelegramException

import config
import local_settings
import app_daily_market_scheduler
import core_market_data_store
from app_bot_reports import quick_report, run_compiled_scan
from app_bot_session import (
    CUSTOM_GROUP_CODE,
    NEUTRALIZATION_LABELS,
    POSITION_MODE_LABELS,
    STRATEGY_LABELS,
    check_permission,
    clear_direct_input,
    compile_selected_universe,
    get_or_create_session,
    parse_tickers,
    set_direct_input,
    sync_config_file,
)
from app_bot_ui import (
    admin_text,
    build_admin_backfill_confirm_markup,
    build_back_home_markup,
    build_admin_markup,
    build_home_markup,
    build_neutralization_markup,
    build_position_mode_markup,
    build_settings_markup,
    build_scan_result_markup,
    build_strategy_markup,
    build_universe_markup,
    home_text,
    settings_text,
)


BOT_TOKEN = local_settings.get_setting("BOT_TOKEN", config.BOT_TOKEN)
SYSTEM_DIR_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONFIG_PATH = f"{SYSTEM_DIR_NAME}/config.local.py"
if not BOT_TOKEN:
    raise RuntimeError(f"Set BOT_TOKEN in ignored {LOCAL_CONFIG_PATH} before starting the bot.")

bot = telebot.TeleBot(BOT_TOKEN)


def permission_message(chat_id):
    return (
        "This chat is not allowed for this test bot.\n"
        f"chat_id: `{chat_id}`\n\n"
        f"Add this chat_id to ALLOWED_CHAT_IDS in {LOCAL_CONFIG_PATH}, "
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

    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_length:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.extend(
                line[i : i + max_length] for i in range(0, len(line), max_length)
            )
        elif len(current) + len(line) > max_length:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
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

        elif data == "settings_neutralize":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Neutralization Settings**\n\n"
                "Subtract the selected group mean from today's cross-sectional candidate score.\n"
                "Country uses listing-market groups only: US / CH / JP / KR.\n"
                "Singleton groups become zero; missing labels use a parent/Unclassified group.\n"
                "The same entry-date selection is applied to IS/OOS; exits remain strategy-specific.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_neutralization_markup(chat_id),
            )

        elif data == "settings_position_mode":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Position Mode Settings**\n\n"
                "Long Only: allow LONG candidates and trades only.\n"
                "Long / Short Neutral: allow both directions symmetrically.\n"
                "Short Only: allow SHORT candidates and trades only.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_position_mode_markup(chat_id),
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

        elif data.startswith("set_neutralize:"):
            mode = data.split(":", 1)[1]
            if mode in NEUTRALIZATION_LABELS:
                session["neutralization"] = mode
            bot.answer_callback_query(call.id, "Neutralization updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_neutralization_markup(chat_id),
            )

        elif data.startswith("set_position_mode:"):
            mode = data.split(":", 1)[1]
            if mode in POSITION_MODE_LABELS:
                session["position_mode"] = mode
            bot.answer_callback_query(call.id, "Position mode updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_position_mode_markup(chat_id),
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
            session["last_backtest_detail"] = None
            session["last_backtest_csv_path"] = None
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
                markup=build_scan_result_markup(
                    has_detail=bool(session.get("last_backtest_detail"))
                ),
            )

        elif data == "scan_detail":
            detail = session.get("last_backtest_detail")
            csv_path = session.get("last_backtest_csv_path")
            if not detail:
                bot.answer_callback_query(
                    call.id,
                    "No detail is available. Run the scan again.",
                    show_alert=True,
                )
                return
            bot.answer_callback_query(call.id, "Sending backtest detail and CSV.")
            send_long_message(
                chat_id,
                detail,
                markup=build_back_home_markup(),
            )
            if csv_path and os.path.isfile(csv_path):
                with open(csv_path, "rb") as csv_file:
                    bot.send_document(
                        chat_id,
                        csv_file,
                        caption="Full-universe backtest trade events (IS/OOS CSV)",
                    )

        elif data in {"admin_backfill_all_confirm", "admin_preload_all"}:
            all_tickers = sorted(set(config.universe))
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Confirm Full 4Y Backfill**\n\n"
                f"Scope: all configured tickers ({len(all_tickers)})\n"
                "This downloads four years for every ticker and may take several minutes. "
                "Use Update All (Latest) for routine daily maintenance.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_backfill_confirm_markup("all"),
            )

        elif data == "admin_backfill_selected_confirm":
            selected = compile_selected_universe(session)
            if not selected:
                bot.answer_callback_query(call.id, "No selected tickers.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Confirm Selected 4Y Backfill**\n\n"
                f"Scope: selected universe ({len(selected)} tickers)\n"
                "This downloads four years for every selected ticker. "
                "Use Update Selected (Latest) for routine daily maintenance.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_backfill_confirm_markup("selected"),
            )

        elif data == "admin_refresh_all":
            all_tickers = sorted(set(config.universe))
            bot.answer_callback_query(call.id, "Updating latest data for all tickers.")
            bot.edit_message_text(
                f"Updating latest data ({len(all_tickers)} tickers)...",
                chat_id,
                call.message.message_id,
            )
            summary = core_market_data_store.refresh_latest_price_data(
                all_tickers,
                min_period=core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
            )
            report = core_market_data_store.format_cache_summary(
                summary, title="All-Ticker Latest Update Complete"
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
                f"Updating latest selected-universe data ({len(selected)} tickers)...",
                chat_id,
                call.message.message_id,
            )
            summary = core_market_data_store.refresh_latest_price_data(
                selected,
                min_period=core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
            )
            report = core_market_data_store.format_cache_summary(
                summary, title="Selected-Universe Latest Update Complete"
            )
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data in {"admin_backfill_selected_run", "admin_backfill_all_run"}:
            if data == "admin_backfill_selected_run":
                target = compile_selected_universe(session)
                scope_label = "selected universe"
            else:
                target = sorted(set(config.universe))
                scope_label = "all configured tickers"
            if not target:
                bot.answer_callback_query(call.id, "No target tickers.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "Starting confirmed 4Y backfill.")
            bot.edit_message_text(
                f"Backfilling four years for {scope_label} ({len(target)} tickers)...",
                chat_id,
                call.message.message_id,
            )
            summary = core_market_data_store.backfill_price_data(
                target,
                period=core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
            )
            report = core_market_data_store.format_cache_summary(
                summary, title="Confirmed 4Y Backfill Complete"
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
                f"{core_market_data_store.freshness_summary(all_tickers)}\n"
                f"DB path: `{core_market_data_store.DB_PATH}`"
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_refresh_market_countries":
            bot.answer_callback_query(call.id, "Refreshing listing-market countries.")
            bot.edit_message_text(
                "Classifying cached tickers into US / CH / JP / KR...",
                chat_id,
                call.message.message_id,
            )
            summary = core_market_data_store.refresh_listing_country_labels_from_cache()
            bot.edit_message_text(
                core_market_data_store.format_market_country_summary(summary),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_refresh_classifications":
            all_tickers = sorted(set(config.universe))
            bot.answer_callback_query(call.id, "Refreshing sector and industry metadata.")
            bot.edit_message_text(
                f"Refreshing sector/industry labels ({len(all_tickers)} tickers)...",
                chat_id,
                call.message.message_id,
            )
            summary = core_market_data_store.refresh_security_metadata(all_tickers)
            bot.edit_message_text(
                core_market_data_store.format_metadata_summary(summary),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "await_quick":
            session["awaiting"] = "quick"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send up to 5 tickers for Quick Check.\nExample: `PANW MU TSLA`",
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
            if len(tickers) > 5:
                bot.reply_to(
                    message,
                    "Quick Check supports up to 5 tickers at once. Please try again with 5 or fewer.",
                    reply_markup=build_back_home_markup(),
                )
                return
            bot.reply_to(
                message,
                f"Running Quick Check for {len(tickers)} ticker(s): {', '.join(tickers)}...",
            )
            reports = []
            for idx, ticker in enumerate(tickers, start=1):
                report = quick_report(ticker)
                if len(tickers) > 1:
                    report = f"**[{idx}/{len(tickers)}]**\n" + report
                reports.append(report)
            send_long_message(
                chat_id,
                "\n\n----------------------------------------\n\n".join(reports),
                markup=build_back_home_markup(),
            )
    except Exception as e:
        session["awaiting"] = None
        bot.reply_to(message, f"Error: {e}", reply_markup=build_back_home_markup())


if __name__ == "__main__":
    print("[DEBUG] Quant Bot server started.")
    app_daily_market_scheduler.start_scheduler_thread()
    bot.infinity_polling()
