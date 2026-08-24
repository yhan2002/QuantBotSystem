# File name: bot_session.py
# Manages user sessions, universe selection, and config synchronization.
import os
import importlib
import copy
import pickle
import re
import time
import uuid
import zlib

import config
from core_position_mode import POSITION_MODE_LABELS
import runtime_settings


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
    ("NASDAQ_TOP_30_20260731", "Nasdaq Top 30"),
    ("US_LARGE_CAP_100_20260630_PROXY", "US Top 100"),
]

STRATEGY_LABELS = {
    "MA_CROSS": "Momentum",
    "BOX_RANGE": "Box Range",
    "RELATIVE_EQUILIBRIUM": "Relative Equilibrium",
    "VALUE_REVERSION": "Reversion",
}

BOX_HYBRID_EXIT_POLICY = "BOX_HYBRID_5D"
BOX_HYBRID_EXIT_SESSIONS = 5
BOX_BREAKOUT_ENTRY_POLICY = "BOX_BREAKOUT_ENTRY"
BOX_ONLY_EXIT_POLICIES = {BOX_HYBRID_EXIT_POLICY, BOX_BREAKOUT_ENTRY_POLICY}

EXIT_POLICY_LABELS = {
    "NATIVE": "Strategy Default",
    "FIXED_5D": "D+5 Close",
    BOX_HYBRID_EXIT_POLICY: "Box TP/SL + D+5",
    BOX_BREAKOUT_ENTRY_POLICY: "Box Breakout Fill + TP/SL",
}

EXIT_POLICY_SESSIONS = {
    "NATIVE": None,
    "FIXED_5D": 5,
}

NEUTRALIZATION_LABELS = {
    "NONE": "None",
    "MARKET": "Market",
    "COUNTRY": "Country",
    "SECTOR": "Sector",
    "INDUSTRY": "Industry",
}

USER_SESSIONS = {}
MAX_BACKTEST_REPORTS_PER_CHAT = 8
MAX_BACKTEST_REPORT_BYTES_PER_CHAT = 32 * 1024 * 1024
BACKTEST_REPORT_TTL_SECONDS = 24 * 60 * 60

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
            "exit_policy": "NATIVE",
            "neutralization": "NONE",
            "position_mode": "LONG_SHORT_NEUTRAL",
            "custom_tickers": [],
            "scheduler_active": True,
            "awaiting": None,
            "last_backtest_detail": None,
            "last_backtest_csv_path": None,
            "last_backtest_export_spec": None,
            "last_backtest_export_blob": None,
            "last_backtest_report_id": None,
            "backtest_reports": {},
        }
    session = USER_SESSIONS[chat_id]
    if session.get("strategy") not in STRATEGY_LABELS:
        session["strategy"] = "MA_CROSS"
    if session.get("exit_policy") not in EXIT_POLICY_LABELS:
        session["exit_policy"] = "NATIVE"
    if (
        session["exit_policy"] in BOX_ONLY_EXIT_POLICIES
        and session["strategy"] != "BOX_RANGE"
    ):
        session["exit_policy"] = "NATIVE"
    return session


def _freeze_export_spec(export_spec):
    if export_spec is None:
        return None
    return zlib.compress(
        pickle.dumps(export_spec, protocol=pickle.HIGHEST_PROTOCOL),
        level=3,
    )


def _thaw_export_spec(export_blob):
    if export_blob is None:
        return None
    return pickle.loads(zlib.decompress(export_blob))


def _report_payload_size(payload):
    return (
        len((payload.get("detail") or "").encode("utf-8"))
        + len((payload.get("export_path") or "").encode("utf-8"))
        + len(payload.get("_export_blob") or b"")
    )


def _prune_backtest_reports(session, now=None):
    reports = session.get("backtest_reports") or {}
    now = time.time() if now is None else float(now)
    expired_ids = [
        report_id
        for report_id, payload in reports.items()
        if now - float(payload.get("_created_at", now))
        > BACKTEST_REPORT_TTL_SECONDS
    ]
    for report_id in expired_ids:
        reports.pop(report_id, None)

    while len(reports) > MAX_BACKTEST_REPORTS_PER_CHAT:
        reports.pop(next(iter(reports)))
    while (
        len(reports) > 1
        and sum(_report_payload_size(payload) for payload in reports.values())
        > MAX_BACKTEST_REPORT_BYTES_PER_CHAT
    ):
        reports.pop(next(iter(reports)))


def _public_backtest_report(payload):
    if not payload:
        return None
    if "_export_blob" in payload:
        export_spec = _thaw_export_spec(payload.get("_export_blob"))
    else:
        export_spec = copy.deepcopy(payload.get("export_spec"))
    return {
        "detail": payload.get("detail"),
        "export_path": payload.get("export_path"),
        "export_spec": export_spec,
    }


def store_backtest_report(
    session,
    detail,
    export_path=None,
    export_spec=None,
):
    """Store one immutable result payload and return its callback-safe id."""
    export_blob = _freeze_export_spec(export_spec)
    session["last_backtest_detail"] = detail
    session["last_backtest_csv_path"] = export_path
    session["last_backtest_export_spec"] = None
    session["last_backtest_export_blob"] = export_blob
    if not detail and not export_path and export_spec is None:
        session["last_backtest_report_id"] = None
        return None

    reports = session.setdefault("backtest_reports", {})
    report_id = uuid.uuid4().hex[:12]
    while report_id in reports:
        report_id = uuid.uuid4().hex[:12]
    reports[report_id] = {
        "detail": detail,
        "export_path": export_path,
        "_export_blob": export_blob,
        "_created_at": time.time(),
    }
    session["last_backtest_report_id"] = report_id
    _prune_backtest_reports(session)
    return report_id


def get_backtest_report(session, report_id=None):
    """Return the payload tied to one result message, not another scan."""
    _prune_backtest_reports(session)
    reports = session.get("backtest_reports") or {}
    if report_id:
        return _public_backtest_report(reports.get(report_id))
    latest_id = session.get("last_backtest_report_id")
    if latest_id and latest_id in reports:
        return _public_backtest_report(reports[latest_id])
    detail = session.get("last_backtest_detail")
    export_path = session.get("last_backtest_csv_path")
    export_blob = session.get("last_backtest_export_blob")
    export_spec = (
        _thaw_export_spec(export_blob)
        if export_blob is not None
        else copy.deepcopy(session.get("last_backtest_export_spec"))
    )
    if not detail and not export_path and export_spec is None:
        return None
    return {
        "detail": detail,
        "export_path": export_path,
        "export_spec": export_spec,
    }


def check_permission(chat_id):
    allowed_chat_ids = {
        str(value)
        for value in runtime_settings.get_setting(
            "ALLOWED_CHAT_IDS", config.ALLOWED_CHAT_IDS
        )
    }
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
        new_content, replacements = re.subn(
            pattern,
            universe_string,
            content,
            flags=re.DOTALL,
        )
        if replacements != 1:
            raise RuntimeError(
                "config.py derives universe from universe_groups; "
                "use Settings -> Universe -> Direct Input instead"
            )

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        importlib.reload(config)
        return True
    except Exception as e:
        print(f"[ERROR] config.py sync failed: {e}")
        return False
