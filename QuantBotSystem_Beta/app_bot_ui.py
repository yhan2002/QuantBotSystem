# File name: bot_ui.py
# Builds Telegram button menus and screen text.
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from app_bot_session import (
    CUSTOM_GROUP_CODE,
    NEUTRALIZATION_LABELS,
    POSITION_MODE_LABELS,
    STRATEGY_LABELS,
    UNIVERSE_MENUS,
    compile_selected_universe,
    get_or_create_session,
    selected_universe_label,
)


def build_home_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Today Action Scan", callback_data="run_scan"))
    markup.row(InlineKeyboardButton("Quick Ticker Check", callback_data="await_quick"))
    markup.row(InlineKeyboardButton("Settings", callback_data="settings"))
    markup.row(InlineKeyboardButton("Admin", callback_data="admin"))
    return markup


def build_admin_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("Preload 4Y Market Data", callback_data="admin_preload_all"))
    markup.row(
        InlineKeyboardButton(
            "Refresh Selected Universe", callback_data="admin_refresh_selected"
        )
    )
    markup.row(
        InlineKeyboardButton(
            "Refresh Market Countries (Local)", callback_data="admin_refresh_market_countries"
        )
    )
    markup.row(
        InlineKeyboardButton(
            "Refresh Sector / Industry", callback_data="admin_refresh_classifications"
        )
    )
    markup.row(InlineKeyboardButton("Show Data Cache Status", callback_data="admin_cache_status"))
    markup.row(InlineKeyboardButton("Back", callback_data="go_home"))
    return markup


def admin_text():
    return (
        "**Admin**\n\n"
        "Preload 4Y Market Data: build the history required for fixed IS/OOS tests.\n"
        "Refresh Selected Universe: backfill four years for the fixed 2024-25 IS / 2026 OOS tests.\n"
        "Refresh Market Countries: derive US/CH/JP/KR from cached ticker suffixes (local DB only).\n"
        "Refresh Sector / Industry: refresh sector/industry metadata; market country is also refreshed.\n"
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
    markup.row(InlineKeyboardButton("Neutralize", callback_data="settings_neutralize"))
    markup.row(InlineKeyboardButton("Position Mode", callback_data="settings_position_mode"))
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


def build_neutralization_markup(chat_id):
    session = get_or_create_session(chat_id)
    selected = session.get("neutralization", "NONE")
    markup = InlineKeyboardMarkup()
    for mode, label in NEUTRALIZATION_LABELS.items():
        status = "[x]" if selected == mode else "[ ]"
        markup.row(
            InlineKeyboardButton(
                f"{status} {label}", callback_data=f"set_neutralize:{mode}"
            )
        )
    markup.row(InlineKeyboardButton("Back", callback_data="settings"))
    return markup


def build_position_mode_markup(chat_id):
    session = get_or_create_session(chat_id)
    selected = session.get("position_mode", "LONG_SHORT_NEUTRAL")
    markup = InlineKeyboardMarkup()
    for mode, label in POSITION_MODE_LABELS.items():
        status = "[x]" if selected == mode else "[ ]"
        markup.row(
            InlineKeyboardButton(
                f"{status} {label}", callback_data=f"set_position_mode:{mode}"
            )
        )
    markup.row(InlineKeyboardButton("Back", callback_data="settings"))
    return markup


def build_scan_result_markup(has_detail=False):
    markup = InlineKeyboardMarkup()
    if has_detail:
        markup.row(InlineKeyboardButton("See Detail", callback_data="scan_detail"))
    markup.row(InlineKeyboardButton("Back to Menu", callback_data="go_home"))
    return markup


def home_text(chat_id):
    session = get_or_create_session(chat_id)
    universe = compile_selected_universe(session)
    return (
        "**Quant Bot Menu**\n\n"
        f"Universe: {selected_universe_label(session)} ({len(universe)} tickers)\n"
        f"Strategy: {STRATEGY_LABELS.get(session['strategy'], session['strategy'])}\n"
        f"Neutralize: {NEUTRALIZATION_LABELS.get(session.get('neutralization', 'NONE'), 'None')}\n"
        f"Position: {POSITION_MODE_LABELS.get(session.get('position_mode', 'LONG_SHORT_NEUTRAL'), 'Long / Short Neutral')}\n"
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
        f"Neutralize: {NEUTRALIZATION_LABELS.get(session.get('neutralization', 'NONE'), 'None')}\n"
        f"Position: {POSITION_MODE_LABELS.get(session.get('position_mode', 'LONG_SHORT_NEUTRAL'), 'Long / Short Neutral')}\n"
        f"Scheduler: {'ON' if session.get('scheduler_active', True) else 'OFF'}\n"
        "Data refresh: 08:00 and 19:00 KST\n"
        "Scheduled scan: weekdays 19:10 KST"
    )
