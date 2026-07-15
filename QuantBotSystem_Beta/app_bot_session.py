# File name: bot_session.py
# Manages user sessions, universe selection, and config synchronization.
import os
import importlib
import re

import config
import local_settings


CUSTOM_GROUP_CODE = "CUSTOM"
DEFAULT_GROUPS = {"CH_UNIV", "JP_UNIV", "CSQ_UNIV"}

UNIVERSE_MENUS = [
    ("18C", "18C"),
    ("CH_UNIV", "CH Universe"),
    ("CSQ_CP", "CSQ CP"),
    ("CSQ_MP", "CSQ MP"),
    ("CSQ_SMID", "CSQ SMID"),
    ("CSQ_UNIV", "CSQ UNIV"),
    ("JP_UNIV", "JP Universe"),
    ("US_LARGE_CAP_100_20260630_PROXY", "US Top 100"),
]

STRATEGY_LABELS = {
    "MA_CROSS": "MA Momentum",
    "BOX_RANGE": "Box Range",
    "VALUE_REVERSION": "Price/Liquidity Reversion v2",
}

USER_SESSIONS = {}

MARKET_SUFFIXES = {
    "US": "",
    "USA": "",
    "NYSE": "",
    "NASDAQ": "",
    "NAS": "",
    "HK": ".HK",
    "HKG": ".HK",
    "JP": ".T",
    "JPN": ".T",
    "TOKYO": ".T",
    "TSE": ".T",
    "SH": ".SS",
    "SS": ".SS",
    "SHA": ".SS",
    "SHANGHAI": ".SS",
    "SZ": ".SZ",
    "SHE": ".SZ",
    "SHENZHEN": ".SZ",
    "KR": ".KS",
    "KS": ".KS",
    "KOSPI": ".KS",
    "KQ": ".KQ",
    "KOSDAQ": ".KQ",
}


def normalize_ticker(ticker, market_code=None):
    ticker = ticker.strip().upper()
    if not ticker:
        return ""

    if ":" in ticker and market_code is None:
        left, right = [part.strip().upper() for part in ticker.split(":", 1)]
        if left in MARKET_SUFFIXES:
            return normalize_ticker(right, left)
        return normalize_ticker(left, right)

    if market_code is None:
        return ticker

    market = market_code.strip().upper()
    suffix = MARKET_SUFFIXES.get(market)
    if suffix is None:
        return ticker

    base = ticker.split(".")[0]
    if suffix == ".HK" and base.isdigit():
        base = base.zfill(4)
    return f"{base}{suffix}"


def _parse_ticker_chunk(chunk):
    tokens = [token for token in re.split(r"\s+", chunk.strip()) if token]
    if not tokens:
        return []
    if len(tokens) == 1:
        return [normalize_ticker(tokens[0])]
    if len(tokens) == 2 and tokens[1].upper() in MARKET_SUFFIXES:
        return [normalize_ticker(tokens[0], tokens[1])]
    return [normalize_ticker(token) for token in tokens]


def parse_tickers(text):
    tickers = []
    chunks = [chunk for chunk in re.split(r"[,;\n]+", text.strip()) if chunk.strip()]
    if not chunks:
        chunks = [text]

    for chunk in chunks:
        for ticker in _parse_ticker_chunk(chunk):
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    return tickers


def get_or_create_session(chat_id):
    if chat_id not in USER_SESSIONS:
        USER_SESSIONS[chat_id] = {
            "selected_groups": set(DEFAULT_GROUPS),
            "strategy": "MA_CROSS",
            "custom_tickers": [],
            "scheduler_active": True,
            "awaiting": None,
            "last_backtest_detail": None,
        }
    return USER_SESSIONS[chat_id]


def check_permission(chat_id):
    allowed_chat_ids = {str(value) for value in local_settings.get_setting("ALLOWED_CHAT_IDS", config.ALLOWED_CHAT_IDS)}
    return str(chat_id) in allowed_chat_ids


def get_group_label(group_code):
    if group_code == CUSTOM_GROUP_CODE:
        return "Direct Input"
    for code, label in UNIVERSE_MENUS:
        if code == group_code:
            return label
    return group_code


def selected_universe_label(session):
    labels = [get_group_label(code) for code in sorted(session["selected_groups"])]
    return ", ".join(labels) if labels else "None"


def compile_selected_universe(session):
    compiled = set()
    for group_code in session["selected_groups"]:
        if group_code == CUSTOM_GROUP_CODE:
            compiled.update(session.get("custom_tickers", []))
        elif hasattr(config, "universe_groups") and group_code in config.universe_groups:
            compiled.update(config.universe_groups[group_code])
    return sorted(compiled)


def set_direct_input(session, tickers):
    session["custom_tickers"] = tickers
    session["selected_groups"] = set([CUSTOM_GROUP_CODE])
    session["awaiting"] = None


def clear_direct_input(session):
    session["custom_tickers"] = []
    session["selected_groups"].discard(CUSTOM_GROUP_CODE)
    if not session["selected_groups"]:
        session["selected_groups"].update(DEFAULT_GROUPS)


def sync_config_file(base_dir):
    try:
        config_path = os.path.join(base_dir, "config.py")

        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = r"(universe\s*=\s*\[)(.*?)(\])"
        formatted_tickers = []
        current_universe = sorted(config.universe)

        for i in range(0, len(current_universe), 10):
            chunk = current_universe[i : i + 10]
            items_str = ", ".join(f"'{t}'" for t in chunk)
            formatted_tickers.append(f"    {items_str}")

        universe_string = "universe = [\n" + ",\n".join(formatted_tickers) + "\n]"
        new_content = re.sub(pattern, universe_string, content, flags=re.DOTALL)

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        importlib.reload(config)
        return True
    except Exception as e:
        print(f"[ERROR] config.py sync failed: {e}")
        return False
