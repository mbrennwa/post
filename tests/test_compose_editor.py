# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for compose WebKit editor helpers (#206 Phase A)."""

from __future__ import annotations

import unittest

from post.compose_editor import (
    build_editor_document,
    compose_uri_opens_externally,
    editor_html_is_plain_equivalent,
    normalize_compose_link_url,
)
from post.mail.compose import html_body_fragment, plain_to_simple_html


class BuildEditorDocumentTests(unittest.TestCase):
    def test_escapes_plain_body(self) -> None:
        doc = build_editor_document(body_plain="Hello <script>alert(1)</script> {x}")
        self.assertIn("Hello &lt;script&gt;alert(1)&lt;/script&gt; {x}", doc)
        self.assertIn('contenteditable="true"', doc)
        self.assertIn("window.__postCompose", doc)
        self.assertNotIn("__COMPOSE_BODY__", doc)
        self.assertNotIn("__COMPOSE_HANDLER__", doc)
        self.assertNotIn("__COMPOSE_ACTION__", doc)
        self.assertIn("increaseQuote", doc)
        self.assertIn("beginLink", doc)
        self.assertIn("KeyK", doc)
        self.assertIn("open-link", doc)
        self.assertIn("data-post-href", doc)
        self.assertIn("stampLinks", doc)
        self.assertIn('addEventListener("click"', doc)
        self.assertIn("}, true);", doc)

    def test_html_fragment_path(self) -> None:
        doc = build_editor_document(body_html="<b>Hi</b>")
        self.assertIn("<b>Hi</b>", doc)
        self.assertNotIn("&lt;b&gt;", doc)


class EditorHtmlPlainEquivalentTests(unittest.TestCase):
    def test_plain_to_simple_html(self) -> None:
        plain = "Hello\nWorld"
        self.assertTrue(
            editor_html_is_plain_equivalent(plain_to_simple_html(plain), plain)
        )

    def test_escaped_text(self) -> None:
        plain = "a < b"
        self.assertTrue(editor_html_is_plain_equivalent("a &lt; b", plain))

    def test_br_newlines(self) -> None:
        plain = "a\nb"
        self.assertTrue(editor_html_is_plain_equivalent("a<br>b", plain))

    def test_contenteditable_div_wrappers(self) -> None:
        self.assertTrue(editor_html_is_plain_equivalent("<div>Hello</div>", "Hello"))
        self.assertTrue(
            editor_html_is_plain_equivalent(
                "<div>Hello</div><div>World</div>",
                "Hello\nWorld",
            )
        )
        self.assertTrue(
            editor_html_is_plain_equivalent(
                "<div>Hello</div><div><br></div>",
                "Hello",
            )
        )

    def test_trailing_space_stays_plain(self) -> None:
        # contenteditable often uses a literal trailing space or &nbsp;.
        self.assertTrue(editor_html_is_plain_equivalent("Hello ", "Hello "))
        self.assertTrue(editor_html_is_plain_equivalent("Hello&nbsp;", "Hello "))
        self.assertTrue(editor_html_is_plain_equivalent("Hello&nbsp;", "Hello"))
        self.assertTrue(editor_html_is_plain_equivalent("Hello\xa0", "Hello "))
        self.assertTrue(
            editor_html_is_plain_equivalent("<div>Hello&nbsp;</div>", "Hello ")
        )
        self.assertTrue(
            editor_html_is_plain_equivalent(
                '<span class="Apple-converted-space">&nbsp;</span>',
                " ",
            )
        )

    def test_editor_blocks_file_drop_insertion(self) -> None:
        doc = build_editor_document()
        self.assertIn('addEventListener("drop"', doc)
        self.assertIn("e.preventDefault()", doc)

    def test_real_markup_not_equivalent(self) -> None:
        plain = "Hello"
        self.assertFalse(
            editor_html_is_plain_equivalent("<b>Hello</b>", plain)
        )
        self.assertFalse(
            editor_html_is_plain_equivalent(
                '<div style="font-weight:bold">Hello</div>',
                "Hello",
            )
        )


class NormalizeComposeLinkUrlTests(unittest.TestCase):
    def test_https_passthrough(self) -> None:
        self.assertEqual(
            normalize_compose_link_url("https://example.com/x"),
            "https://example.com/x",
        )

    def test_adds_https_when_missing_scheme(self) -> None:
        self.assertEqual(
            normalize_compose_link_url("example.com"),
            "https://example.com",
        )

    def test_mailto(self) -> None:
        self.assertEqual(
            normalize_compose_link_url("mailto:a@b.example"),
            "mailto:a@b.example",
        )
        self.assertEqual(
            normalize_compose_link_url("a@b.example"),
            "mailto:a@b.example",
        )

    def test_rejects_javascript(self) -> None:
        self.assertIsNone(normalize_compose_link_url("javascript:alert(1)"))
        self.assertIsNone(normalize_compose_link_url("data:text/html,x"))

    def test_empty(self) -> None:
        self.assertIsNone(normalize_compose_link_url("  "))


class ComposeUriOpensExternallyTests(unittest.TestCase):
    def test_allows_http_https_mailto(self) -> None:
        self.assertTrue(compose_uri_opens_externally("https://example.com"))
        self.assertTrue(compose_uri_opens_externally("http://example.com"))
        self.assertTrue(compose_uri_opens_externally("mailto:a@b.example"))

    def test_rejects_unsafe(self) -> None:
        self.assertFalse(compose_uri_opens_externally("javascript:alert(1)"))
        self.assertFalse(compose_uri_opens_externally("file:///tmp/x"))
        self.assertFalse(compose_uri_opens_externally(""))


class HtmlBodyFragmentTests(unittest.TestCase):
    def test_strips_document_wrappers(self) -> None:
        self.assertEqual(
            html_body_fragment("<html><body><b>Hi</b></body></html>"),
            "<b>Hi</b>",
        )


if __name__ == "__main__":
    unittest.main()
