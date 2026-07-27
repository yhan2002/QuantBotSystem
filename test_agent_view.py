from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

from tools.build_agent_view import (
    CONTENT_SECURITY_POLICY,
    DEFAULT_FIXTURE,
    build_view,
    load_fixture,
    render_html,
    validate_fixture,
    validate_rendered_html,
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


if __name__ == "__main__":
    unittest.main()
