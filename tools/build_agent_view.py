"""Build a deterministic synthetic DOM snapshot and optional browser screenshot."""

from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "harness" / "agent-view.json"
DEFAULT_OUTPUT_DIR = ROOT / ".agent-artifacts"
ALLOWED_TONES = {"ok", "warning", "neutral", "blocked"}
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "font-src 'none'; connect-src 'none'"
)
SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|bot[_ -]?token)\s*[:=]\s*\S+"),
)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    return []


def validate_fixture(fixture: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(fixture, dict):
        return ["fixture must be a JSON object"]
    if fixture.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    for key in ("view_id", "title", "subtitle", "notice"):
        if not _nonempty(fixture.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if "SYNTHETIC FIXTURE" not in str(fixture.get("notice", "")):
        errors.append("notice must identify the view as a SYNTHETIC FIXTURE")

    cards = fixture.get("cards")
    if not isinstance(cards, list) or not cards:
        errors.append("cards must be a non-empty list")
        cards = []
    for index, card in enumerate(cards, start=1):
        label = f"card {index}"
        if not isinstance(card, dict):
            errors.append(f"{label} must be a JSON object")
            continue
        if not _nonempty(card.get("label")):
            errors.append(f"{label} label must be a non-empty string")
        if not _nonempty(card.get("value")):
            errors.append(f"{label} value must be a non-empty string")
        if card.get("tone") not in ALLOWED_TONES:
            errors.append(f"{label} tone must be one of {sorted(ALLOWED_TONES)}")

    tables = fixture.get("tables")
    if not isinstance(tables, list) or not tables:
        errors.append("tables must be a non-empty list")
        tables = []
    for table_index, table in enumerate(tables, start=1):
        label = f"table {table_index}"
        if not isinstance(table, dict):
            errors.append(f"{label} must be a JSON object")
            continue
        if not _nonempty(table.get("title")):
            errors.append(f"{label} title must be a non-empty string")
        columns = table.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(_nonempty(column) for column in columns)
        ):
            errors.append(f"{label} columns must be a non-empty string list")
            columns = []
        elif len(set(columns)) != len(columns):
            errors.append(f"{label} columns must be unique")
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            errors.append(f"{label} rows must be a non-empty list")
            rows = []
        if len(rows) > 20:
            errors.append(f"{label} may contain at most 20 rows")
        for row_index, row in enumerate(rows, start=1):
            if (
                not isinstance(row, list)
                or len(row) != len(columns)
                or not all(_nonempty(cell) for cell in row)
            ):
                errors.append(
                    f"{label} row {row_index} must match the columns with strings"
                )

    notes = fixture.get("notes")
    if not isinstance(notes, list) or not notes:
        errors.append("notes must be a non-empty list")
        notes = []
    for index, note in enumerate(notes, start=1):
        label = f"note {index}"
        if not isinstance(note, dict):
            errors.append(f"{label} must be a JSON object")
            continue
        if not _nonempty(note.get("title")):
            errors.append(f"{label} title must be a non-empty string")
        if note.get("tone") not in ALLOWED_TONES:
            errors.append(f"{label} tone must be one of {sorted(ALLOWED_TONES)}")
        items = note.get("items")
        if not isinstance(items, list) or not items or not all(
            _nonempty(item) for item in items
        ):
            errors.append(f"{label} items must be a non-empty string list")

    for text in _all_strings(fixture):
        if "http://" in text or "https://" in text:
            errors.append("fixture must not load or expose external URLs")
        if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS):
            errors.append("fixture contains a value shaped like a credential")
    return sorted(set(errors))


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8-sig"))
    errors = validate_fixture(fixture)
    if errors:
        raise ValueError("; ".join(errors))
    return fixture


def _tone_label(tone: str) -> str:
    return {
        "ok": "OK",
        "warning": "WARNING",
        "neutral": "INFO",
        "blocked": "BLOCKED",
    }[tone]


def render_html(fixture: dict[str, Any]) -> str:
    cards = []
    for card in fixture["cards"]:
        tone = escape(card["tone"])
        cards.append(
            f"""
            <article class="card tone-{tone}">
              <p class="card-label">{escape(card["label"])}</p>
              <p class="card-value">{escape(card["value"])}</p>
              <span class="status">{_tone_label(card["tone"])}</span>
            </article>"""
        )

    tables = []
    for index, table in enumerate(fixture["tables"], start=1):
        header = "".join(
            f'<th scope="col">{escape(column)}</th>' for column in table["columns"]
        )
        rows = "".join(
            "<tr>"
            + "".join(f"<td>{escape(cell)}</td>" for cell in row)
            + "</tr>"
            for row in table["rows"]
        )
        tables.append(
            f"""
            <section class="panel" aria-labelledby="table-{index}-title">
              <h2 id="table-{index}-title">{escape(table["title"])}</h2>
              <div class="table-scroll" tabindex="0">
                <table>
                  <thead><tr>{header}</tr></thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
            </section>"""
        )

    notes = []
    for index, note in enumerate(fixture["notes"], start=1):
        items = "".join(f"<li>{escape(item)}</li>" for item in note["items"])
        notes.append(
            f"""
            <aside class="note tone-{escape(note["tone"])}"
                   aria-labelledby="note-{index}-title">
              <div>
                <h2 id="note-{index}-title">{escape(note["title"])}</h2>
                <span class="status">{_tone_label(note["tone"])}</span>
              </div>
              <ul>{items}</ul>
            </aside>"""
        )

    return f"""<!doctype html>
<html lang="en" data-agent-view="true" data-fixture="synthetic">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="{CONTENT_SECURITY_POLICY}">
  <title>{escape(fixture["title"])}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #152238;
      --muted: #5f6b7a;
      --line: #d8e0ea;
      --surface: #ffffff;
      --canvas: #edf2f7;
      --accent: #174ea6;
      --ok: #157347;
      --warning: #9a6700;
      --blocked: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--canvas);
      color: var(--ink);
      font: 15px/1.45 Inter, "Segoe UI", Arial, sans-serif;
    }}
    header, main, footer {{ width: min(1180px, calc(100% - 40px)); margin-inline: auto; }}
    header {{ padding: 34px 0 22px; }}
    h1 {{ margin: 0; font-size: 34px; letter-spacing: -0.03em; }}
    h2 {{ margin: 0 0 14px; font-size: 19px; }}
    .subtitle {{ margin: 7px 0 0; color: var(--muted); font-size: 17px; }}
    .fixture-notice {{
      margin-top: 18px;
      padding: 11px 14px;
      border: 1px solid #f3c969;
      border-radius: 10px;
      background: #fff8df;
      color: #6f4e00;
      font-weight: 700;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .card, .panel, .note {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--surface);
      box-shadow: 0 5px 16px rgba(28, 45, 70, 0.06);
    }}
    .card {{ min-height: 130px; padding: 18px; border-top: 4px solid var(--accent); }}
    .card.tone-ok {{ border-top-color: var(--ok); }}
    .card.tone-warning {{ border-top-color: var(--warning); }}
    .card.tone-blocked {{ border-top-color: var(--blocked); }}
    .card-label {{ margin: 0; color: var(--muted); font-weight: 650; }}
    .card-value {{ margin: 10px 0 12px; font-size: 24px; font-weight: 760; }}
    .status {{
      display: inline-block;
      border-radius: 999px;
      padding: 3px 8px;
      background: #e9eef6;
      color: #344054;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.05em;
    }}
    .tone-ok .status {{ background: #dff4e9; color: #0f653f; }}
    .tone-warning .status {{ background: #fff0c7; color: #795300; }}
    .tone-blocked .status {{ background: #fee4e2; color: #912018; }}
    .panel {{ margin-bottom: 18px; padding: 20px; }}
    .table-scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 650px; }}
    th, td {{ padding: 11px 13px; border-bottom: 1px solid var(--line); text-align: left; }}
    th {{ background: #f5f7fa; color: #344054; font-size: 12px; text-transform: uppercase; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    .note {{ margin-bottom: 18px; padding: 18px 20px; border-left: 5px solid var(--accent); }}
    .note.tone-warning {{ border-left-color: var(--warning); }}
    .note.tone-blocked {{ border-left-color: var(--blocked); }}
    .note > div {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .note h2 {{ margin: 0; }}
    .note ul {{ margin: 12px 0 0; padding-left: 21px; }}
    footer {{ padding: 2px 0 28px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 820px) {{
      header, main, footer {{ width: min(100% - 24px, 1180px); }}
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      h1 {{ font-size: 27px; }}
    }}
  </style>
</head>
<body>
  <header role="banner">
    <h1>{escape(fixture["title"])}</h1>
    <p class="subtitle">{escape(fixture["subtitle"])}</p>
    <p class="fixture-notice" role="note">{escape(fixture["notice"])}</p>
  </header>
  <main id="main-content">
    <section class="cards" aria-label="Summary status">
      {"".join(cards)}
    </section>
    {"".join(tables)}
    {"".join(notes)}
  </main>
  <footer role="contentinfo">
    Deterministic agent preview · no external assets · no production data
  </footer>
</body>
</html>
"""


def validate_rendered_html(html_text: str) -> list[str]:
    errors: list[str] = []
    lowered = html_text.casefold()
    if (
        'http-equiv="content-security-policy"' not in lowered
        or f'content="{CONTENT_SECURITY_POLICY}"'.casefold() not in lowered
    ):
        errors.append("DOM must contain the strict synthetic-view CSP")
    if "<script" in lowered:
        errors.append("DOM must not contain scripts")
    if re.search(r"(?i)\b(?:src|srcset|href)\s*=", html_text):
        errors.append("DOM must not contain resource or navigation attributes")
    if re.search(r"(?i)(?:https?:)?//", html_text):
        errors.append("DOM must not contain network URLs")
    if "@import" in lowered or re.search(r"(?i)\burl\s*\(", html_text):
        errors.append("DOM CSS must not contain external resource directives")
    return sorted(set(errors))


def build_view(
    fixture_path: Path = DEFAULT_FIXTURE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    fixture = load_fixture(fixture_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "agent-view.dom.html"
    snapshot_path = output_dir / "agent-view.dom.json"

    html_text = render_html(fixture)
    render_errors = validate_rendered_html(html_text)
    if render_errors:
        raise ValueError("; ".join(render_errors))
    canonical_fixture = json.dumps(
        fixture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot = {
        "schema_version": 1,
        "view_id": fixture["view_id"],
        "fixture": "synthetic",
        "title": fixture["title"],
        "landmarks": ["banner", "main", "contentinfo"],
        "headings": [
            fixture["title"],
            *[table["title"] for table in fixture["tables"]],
            *[note["title"] for note in fixture["notes"]],
        ],
        "cards": fixture["cards"],
        "tables": [
            {
                "title": table["title"],
                "columns": table["columns"],
                "row_count": len(table["rows"]),
            }
            for table in fixture["tables"]
        ],
        "fixture_sha256": hashlib.sha256(canonical_fixture).hexdigest(),
        "dom_sha256": hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
    }
    html_path.write_text(html_text, encoding="utf-8", newline="\n")
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return html_path, snapshot_path


def _browser_candidates() -> list[Path]:
    names = (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
        "msedge",
        "microsoft-edge",
    )
    candidates = [Path(found) for name in names if (found := shutil.which(name))]
    if os.name == "nt":
        candidates.extend(
            Path(path)
            for path in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            )
            if Path(path).is_file()
        )
    return candidates


def find_browser(explicit: Path | None = None) -> Path:
    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"browser executable not found: {resolved}")
        return resolved
    candidates = _browser_candidates()
    if not candidates:
        raise FileNotFoundError(
            "No Chromium-based browser found. Use --browser with an executable path."
        )
    return candidates[0]


def capture_screenshot(
    html_path: Path,
    screenshot_path: Path,
    browser_path: Path | None = None,
) -> Path:
    browser = find_browser(browser_path)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-view-browser-") as workspace:
        workspace_path = Path(workspace)
        profile = workspace_path / "profile"
        staged_html = workspace_path / "agent-view.html"
        staged_screenshot = workspace_path / "agent-view.png"
        shutil.copyfile(html_path, staged_html)
        command = [
            str(browser),
            "--headless=new",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-sync",
            "--hide-scrollbars",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-sandbox",
            "--force-device-scale-factor=1",
            "--run-all-compositor-stages-before-draw",
            "--window-size=1440,1100",
            f"--user-data-dir={profile}",
            f"--screenshot={staged_screenshot}",
            staged_html.as_uri(),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if (
            result.returncode != 0
            or not staged_screenshot.is_file()
            or staged_screenshot.stat().st_size == 0
        ):
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"browser screenshot failed with exit {result.returncode}: {detail}"
            )
        shutil.copy2(staged_screenshot, screenshot_path)
    return screenshot_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--screenshot", action="store_true")
    parser.add_argument("--browser", type=Path)
    args = parser.parse_args(argv)

    try:
        html_path, snapshot_path = build_view(args.fixture, args.output_dir)
        print(f"DOM: {html_path}")
        print(f"Snapshot: {snapshot_path}")
        if args.screenshot:
            screenshot_path = capture_screenshot(
                html_path,
                args.output_dir / "agent-view.png",
                args.browser,
            )
            print(f"Screenshot: {screenshot_path}")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Agent view build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
