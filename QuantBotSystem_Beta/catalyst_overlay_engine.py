# File name: catalyst_overlay_engine.py
# Builds Telegram reports for the fixed scalping catalyst overlay.
import os

import config
import scalping_catalyst_engine as catalyst


def _resolve_workbook_path():
    path = getattr(config, "SCALPING_WORKBOOK_PATH", "")
    if not path:
        path = "../스캘핑 ver 2.0 - 복사본.xlsx"
    if os.path.isabs(path):
        candidates = [path]
    else:
        base_dir = os.path.dirname(__file__)
        candidates = [
            os.path.abspath(os.path.join(base_dir, path)),
            os.path.abspath(os.path.join(base_dir, "../스캘핑 ver 2.0 - 복사본.xlsx")),
            os.path.abspath(os.path.join(base_dir, "../scalping_sandbox.xlsx")),
        ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def _latest_records_by_ticker(records):
    latest = {}
    for record in records:
        ticker = record.get("ticker")
        event_date = record.get("event_date")
        if not ticker or not event_date:
            continue
        if ticker not in latest or event_date > latest[ticker].get("event_date", ""):
            latest[ticker] = record
    return latest


def _drivers(record):
    pairs = [
        ("1D > 7%", record.get("is_7pct_up")),
        ("52W high", record.get("is_52w_high")),
        ("estimate up", record.get("is_estimate_up")),
        ("turnover top10", record.get("is_turnover_top10")),
        ("52W low", record.get("is_52w_low")),
    ]
    return [label for label, active in pairs if active]


def scan_catalyst_fixed(universe):
    workbook_path = _resolve_workbook_path()
    if not os.path.exists(workbook_path):
        return (
            "Catalyst Fixed Scan\n\n"
            f"Workbook not found: `{workbook_path}`\n"
            "Set SCALPING_WORKBOOK_PATH in config.py."
        )

    universe_set = set(universe)
    records = catalyst.load_scalping_events(workbook_path)
    latest = _latest_records_by_ticker(records)

    candidates = []
    for ticker in sorted(universe_set):
        record = latest.get(ticker)
        if not record:
            continue
        fixed_score = catalyst.fixed_catalyst_score(record)
        if fixed_score < catalyst.FIXED_THRESHOLD:
            continue
        enriched = dict(record)
        enriched["fixed_score"] = fixed_score
        candidates.append(enriched)

    candidates.sort(
        key=lambda r: (r.get("event_date", ""), r.get("fixed_score", 0.0)),
        reverse=True,
    )

    report = "Catalyst Fixed Scan\n"
    report += "Scalping catalyst overlay, test version\n"
    report += "=" * 29 + "\n\n"
    report += (
        f"Parameters: obj:ana "
        f"{catalyst.FIXED_OBJ_WEIGHT:.0%}:{catalyst.FIXED_ANA_WEIGHT:.0%}, "
        f"analyst cap {catalyst.ANALYST_CAP}, "
        f"threshold >= {catalyst.FIXED_THRESHOLD}, "
        f"holding reference {catalyst.FIXED_HOLDING_DAYS}d\n"
    )
    report += (
        "Backtest reference: count 1247, avg +1.48%, "
        "std 11.52%, t 4.53, win 53.2%\n\n"
    )

    if not candidates:
        report += "No catalyst-confirmed names in the selected universe."
        return report

    report += "Catalyst Confirmed\n"
    for idx, record in enumerate(candidates[:20], start=1):
        drivers = ", ".join(_drivers(record)) or "analyst score"
        report += (
            f"{idx}. {record['ticker']:<10} | score {record['fixed_score']:.1f} "
            f"| date {record['event_date']} | {drivers}\n"
        )

    if len(candidates) > 20:
        report += f"\n... +{len(candidates) - 20} more\n"

    report += "\nInterpretation: use as confirmation/filter, not standalone trade advice."
    return report
