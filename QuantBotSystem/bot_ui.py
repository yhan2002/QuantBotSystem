# File name: bot_ui.py
# Builds Telegram button menus and screen text.
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_session import (
    CUSTOM_GROUP_CODE,
    STRATEGY_LABELS,
    UNIVERSE_MENUS,
    compile_selected_universe,
    get_or_create_session,
    selected_universe_label,
)


def build_home_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Today Action Scan", callback_data="run_scan"))
    markup.row(
        InlineKeyboardButton("Quick Ticker Check", callback_data="await_quick"),
        InlineKeyboardButton("Box TP/SL Test", callback_data="await_boxbt"),
    )
    markup.row(InlineKeyboardButton("Settings", callback_data="settings"))
    markup.row(InlineKeyboardButton("Admin", callback_data="admin"))
    return markup


def build_admin_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Preload All Market Data", callback_data="admin_preload_all"))
    markup.row(
        InlineKeyboardButton(
            "Refresh Selected Universe", callback_data="admin_refresh_selected"
        )
    )
    markup.row(InlineKeyboardButton("Show Data Cache Status", callback_data="admin_cache_status"))
    markup.row(InlineKeyboardButton("Back", callback_data="go_home"))
    return markup


def admin_text():
    return (
        "**Admin**\n\n"
        "Preload All Market Data: update every configured universe ticker.\n"
        "Refresh Selected Universe: update only the current menu selection.\n"
        "Show Data Cache Status: show the current cache date range."
    )


def build_settings_markup(chat_id):
    session = get_or_create_session(chat_id)
    scheduler_label = (
        "Scheduler: ON" if session.get("scheduler_active", True) else "Scheduler: OFF"
    )
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Universe", callback_data="settings_universe"))
    markup.row(InlineKeyboardButton("Strategy", callback_data="settings_strategy"))
    markup.row(InlineKeyboardButton(scheduler_label, callback_data="toggle_scheduler"))
    markup.row(InlineKeyboardButton("Back", callback_data="go_home"))
    return markup


def build_universe_markup(chat_id):
    session = get_or_create_session(chat_id)
    selected = session["selected_groups"]
    markup = InlineKeyboardMarkup()

    menus = UNIVERSE_MENUS + [(CUSTOM_GROUP_CODE, "Direct Input")]
    for i in range(0, len(menus), 2):
        row = []
        for code, label in menus[i : i + 2]:
            status = "[x]" if code in selected else "[ ]"
            row.append(
                InlineKeyboardButton(
                    f"{status} {label}", callback_data=f"toggle_group:{code}"
                )
            )
        markup.row(*row)

    markup.row(
        InlineKeyboardButton("Set Direct Input", callback_data="await_custom_universe"),
        InlineKeyboardButton("Clear Direct Input", callback_data="clear_custom"),
    )
    markup.row(InlineKeyboardButton("Back", callback_data="settings"))
    return markup


def build_strategy_markup(chat_id):
    session = get_or_create_session(chat_id)
    markup = InlineKeyboardMarkup()
    for strategy, label in STRATEGY_LABELS.items():
        status = "[x]" if session["strategy"] == strategy else "[ ]"
        markup.row(
            InlineKeyboardButton(
                f"{status} {label}", callback_data=f"set_strategy:{strategy}"
            )
        )
    markup.row(InlineKeyboardButton("Back", callback_data="settings"))
    return markup


def build_back_home_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Back to Menu", callback_data="go_home"))
    return markup


def home_text(chat_id):
    session = get_or_create_session(chat_id)
    universe = compile_selected_universe(session)
    return (
        "**Quant Bot Menu**\n\n"
        f"Universe: {selected_universe_label(session)} ({len(universe)} tickers)\n"
        f"Strategy: {STRATEGY_LABELS.get(session['strategy'], session['strategy'])}\n"
        f"Scheduler: {'ON' if session.get('scheduler_active', True) else 'OFF'}\n"
        "Data refresh: 08:00 and 19:00 KST\n"
        "Scheduled scan: weekdays 19:10 KST\n\n"
        "Choose what you want to do."
    )


def settings_text(chat_id):
    session = get_or_create_session(chat_id)
    custom = ", ".join(session.get("custom_tickers", [])) or "None"
    return (
        "**Settings**\n\n"
        f"Universe: {selected_universe_label(session)}\n"
        f"Direct Input: {custom}\n"
        f"Strategy: {STRATEGY_LABELS.get(session['strategy'], session['strategy'])}\n"
        f"Scheduler: {'ON' if session.get('scheduler_active', True) else 'OFF'}\n"
        "Data refresh: 08:00 and 19:00 KST\n"
        "Scheduled scan: weekdays 19:10 KST"
    )
