from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib

from tools.build_agent_view import (
    CONTENT_SECURITY_POLICY,
    DEFAULT_FIXTURE,
    PNG_IEND_CHUNK,
    PNG_SIGNATURE,
    _is_complete_png,
    _wait_for_complete_png,
    build_view,
    load_fixture,
    render_html,
    validate_fixture,
    validate_rendered_html,
)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def _minimal_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    scanline = zlib.compress(b"\x00\x00\x00\x00\x00")
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", scanline)
        + PNG_IEND_CHUNK
    )


class _DomContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.roles: set[str] = set()
        self.ids: set[str] = set()
        self.table_count = 0
        self.external_references: list[str] = []
        self.content_security_policy: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append(tag)
        values = dict(attrs)
        if values.get("role"):
            self.roles.add(str(values["role"]))
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "table":
            self.table_count += 1
        if (
            tag == "meta"
            and str(values.get("http-equiv", "")).casefold()
            == "content-security-policy"
        ):
            self.content_security_policy = values.get("content")
        for name in ("href", "src", "srcset"):
            value = values.get(name)
            if value:
                self.external_references.append(value)


class AgentViewTest(unittest.TestCase):
    def test_fixture_renders_safe_semantic_dom(self) -> None:
        fixture = load_fixture(DEFAULT_FIXTURE)
        html = render_html(fixture)
        parser = _DomContractParser()
        parser.feed(html)

        self.assertIn("header", parser.tags)
        self.assertIn("main", parser.tags)
        self.assertIn("footer", parser.tags)
        self.assertIn("banner", parser.roles)
        self.assertIn("contentinfo", parser.roles)
        self.assertIn("main-content", parser.ids)
        self.assertEqual(len(fixture["tables"]), parser.table_count)
        self.assertEqual([], parser.external_references)
        self.assertEqual(CONTENT_SECURITY_POLICY, parser.content_security_policy)
        self.assertEqual([], validate_rendered_html(html))
        self.assertNotIn("<script", html.lower())
        self.assertIn('data-fixture="synthetic"', html)

    def test_build_is_deterministic_and_snapshot_matches_fixture(self) -> None:
        fixture = load_fixture(DEFAULT_FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            html_path, snapshot_path = build_view(DEFAULT_FIXTURE, output_dir)
            first_html = html_path.read_bytes()
            first_snapshot = snapshot_path.read_bytes()
            build_view(DEFAULT_FIXTURE, output_dir)

            self.assertEqual(first_html, html_path.read_bytes())
            self.assertEqual(first_snapshot, snapshot_path.read_bytes())
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(fixture["view_id"], snapshot["view_id"])
            self.assertEqual("synthetic", snapshot["fixture"])
            self.assertEqual(
                [len(table["rows"]) for table in fixture["tables"]],
                [table["row_count"] for table in snapshot["tables"]],
            )
            self.assertEqual(64, len(snapshot["fixture_sha256"]))
            self.assertEqual(64, len(snapshot["dom_sha256"]))

    def test_invalid_fixture_is_rejected(self) -> None:
        fixture = load_fixture(DEFAULT_FIXTURE)
        fixture["notice"] = "production"
        fixture["tables"][0]["rows"][0] = ["too", "short"]
        errors = validate_fixture(fixture)
        self.assertIn(
            "notice must identify the view as a SYNTHETIC FIXTURE",
            errors,
        )
        self.assertIn(
            "table 1 row 1 must match the columns with strings",
            errors,
        )

    def test_network_capable_dom_is_rejected(self) -> None:
        unsafe_html = """
        <meta http-equiv="Content-Security-Policy"
              content="default-src 'none'">
        <style>@IMPORT "HTTPS://example.test/base.css";
        .card { background: URL(//example.test/pixel.png); }</style>
        <img SRC="https://example.test/image.png">
        """
        errors = validate_rendered_html(unsafe_html)
        self.assertIn("DOM must contain the strict synthetic-view CSP", errors)
        self.assertIn(
            "DOM must not contain resource or navigation attributes",
            errors,
        )
        self.assertIn("DOM must not contain network URLs", errors)
        self.assertIn(
            "DOM CSS must not contain external resource directives",
            errors,
        )

    def test_delayed_png_write_waits_for_complete_stable_file(self) -> None:
        complete_png = _minimal_png()
        with tempfile.TemporaryDirectory() as directory:
            screenshot_path = Path(directory) / "delayed.png"
            screenshot_path.write_bytes(complete_png[: -len(PNG_IEND_CHUNK)])

            def finish_write() -> None:
                time.sleep(0.15)
                with screenshot_path.open("ab") as screenshot:
                    screenshot.write(PNG_IEND_CHUNK)

            writer = threading.Thread(target=finish_write)
            writer.start()
            started = time.monotonic()
            try:
                _wait_for_complete_png(
                    screenshot_path,
                    timeout_seconds=2,
                    poll_interval=0.02,
                )
            finally:
                writer.join(timeout=2)

            self.assertGreaterEqual(time.monotonic() - started, 0.1)
            self.assertTrue(_is_complete_png(screenshot_path))


if __name__ == "__main__":
    unittest.main()
