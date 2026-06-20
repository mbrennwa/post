# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.reader import build_reader_document


class BuildReaderDocumentTests(unittest.TestCase):
    def test_plain_body_is_escaped(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain="<script>alert(1)</script>",
            allow_remote=False,
        )
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", doc)
        self.assertNotIn("<script>alert(1)</script>", doc)

    def test_blocks_remote_images_when_disabled(self) -> None:
        html = '<p>Hi</p><img src="https://tracker.example/pixel.png">'
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
        )
        self.assertIn('src=""', doc)
        self.assertIn("Remote images are hidden", doc)
        self.assertNotIn("https://tracker.example/pixel.png", doc)

    def test_no_remote_notice_without_remote_images(self) -> None:
        html = "<p>Hello</p><img src=\"cid:logo@local\">"
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
        )
        self.assertNotIn("Remote images are hidden", doc)

    def test_no_remote_notice_for_css_background_only(self) -> None:
        html = (
            '<table><tr><td style="background-image: url(https://example.com/bg.png)">'
            "View message</td></tr></table>"
        )
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
        )
        self.assertNotIn("Remote images are hidden", doc)
        self.assertNotIn("https://example.com/bg.png", doc)

    def test_no_remote_notice_for_tracking_pixel(self) -> None:
        html = (
            '<p>Hello</p><img width="1" height="1" '
            'src="https://tracker.example/pixel.gif">'
        )
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
        )
        self.assertNotIn("Remote images are hidden", doc)

    def test_no_remote_notice_for_plain_text(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain="Plain text only",
            allow_remote=False,
        )
        self.assertNotIn("Remote images are hidden", doc)

    def test_allows_remote_images_when_enabled(self) -> None:
        url = "https://cdn.example/logo.png"
        html = f'<img src="{url}">'
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=True,
        )
        self.assertIn(url, doc)
        self.assertNotIn("Remote images are hidden", doc)

    def test_empty_body_placeholder(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain=None,
            allow_remote=False,
        )
        self.assertIn("(No message body)", doc)


if __name__ == "__main__":
    unittest.main()
