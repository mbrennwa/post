# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for compose WebKit editor helpers (#206 Phase A)."""

from __future__ import annotations

import unittest

from post.compose_editor import (
    build_editor_document,
    editor_html_is_plain_equivalent,
)
from post.mail.compose import plain_to_simple_html


class BuildEditorDocumentTests(unittest.TestCase):
    def test_escapes_plain_body(self) -> None:
        doc = build_editor_document(body_plain="Hello <script>alert(1)</script> {x}")
        self.assertIn("Hello &lt;script&gt;alert(1)&lt;/script&gt; {x}", doc)
        self.assertIn('contenteditable="true"', doc)
        self.assertIn("window.__postCompose", doc)
        self.assertNotIn("__COMPOSE_BODY__", doc)

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

    def test_editor_blocks_file_drop_insertion(self) -> None:
        doc = build_editor_document()
        self.assertIn('addEventListener("drop"', doc)
        self.assertIn("e.preventDefault()", doc)

    def test_real_markup_not_equivalent(self) -> None:
        plain = "Hello"
        self.assertFalse(
            editor_html_is_plain_equivalent("<b>Hello</b>", plain)
        )


if __name__ == "__main__":
    unittest.main()
