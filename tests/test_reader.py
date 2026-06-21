# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import unittest

from post.preferences import (
    MESSAGE_APPEARANCE_ACCEPT_SENDER,
    MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
    MESSAGE_APPEARANCE_ADAPT_TEXT,
)
from post.reader import build_reader_document
from post.reader.html import resolve_cid_images


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

    def test_reader_uses_light_colors_by_default(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain="Hello",
            allow_remote=False,
        )
        self.assertIn('name="color-scheme" content="light"', doc)
        self.assertIn("color: #1e1e1e", doc)
        self.assertIn("background: #ffffff", doc)

    def test_reader_uses_dark_colors_when_requested(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain="Hello",
            allow_remote=False,
            dark=True,
        )
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("color: #eeeeee", doc)
        self.assertIn("background: #1e1e1e", doc)

    def test_html_email_keeps_inline_styles(self) -> None:
        doc = build_reader_document(
            body_html='<div style="background:#ffffff"><p>Hello</p></div>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ACCEPT_SENDER,
        )
        self.assertIn('style="background:#ffffff"', doc)
        self.assertIn("background: #1e1e1e", doc)
        self.assertNotIn("message-body", doc)

    def test_adapt_text_wraps_html_by_default(self) -> None:
        doc = build_reader_document(
            body_html="<p>Hello</p>",
            body_plain=None,
            allow_remote=False,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_dark_shell_overrides_dark_inline_text(self) -> None:
        doc = build_reader_document(
            body_html='<p style="color:#000000">Hello</p>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("background: #1e1e1e", doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_light_shell_overrides_light_inline_text(self) -> None:
        doc = build_reader_document(
            body_html='<p style="color:#ffffff">Hello</p>',
            body_plain=None,
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn('name="color-scheme" content="light"', doc)
        self.assertIn("background: #ffffff", doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_background_uses_light_shell_in_dark_app(self) -> None:
        doc = build_reader_document(
            body_html='<p style="color:#000000">Hello</p>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('name="color-scheme" content="light"', doc)
        self.assertIn("background: #ffffff", doc)
        self.assertNotIn("message-body", doc)

    def test_adapt_background_uses_dark_shell_in_light_app(self) -> None:
        doc = build_reader_document(
            body_html='<p style="color:#ffffff">Hello</p>',
            body_plain=None,
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("background: #1e1e1e", doc)
        self.assertNotIn("message-body", doc)

    def test_cid_images_are_embedded_as_data_urls(self) -> None:
        png = b"\x89PNG\r\n\x1a\n"
        html = '<img src="cid:logo@local">'
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
            inline_images={"logo@local": ("image/png", png)},
        )
        expected = base64.b64encode(png).decode("ascii")
        self.assertIn(f"data:image/png;base64,{expected}", doc)
        self.assertNotIn("cid:logo@local", doc)

    def test_cid_css_background_is_embedded(self) -> None:
        png = b"\x89PNG\r\n\x1a\n"
        html = '<div style="background-image: url(cid:logo@local)"></div>'
        doc = build_reader_document(
            body_html=html,
            body_plain=None,
            allow_remote=False,
            inline_images={"logo@local": ("image/png", png)},
        )
        expected = base64.b64encode(png).decode("ascii")
        self.assertIn(f"url(data:image/png;base64,{expected})", doc)


class ResolveCidImagesTests(unittest.TestCase):
    def test_matches_angle_bracket_content_id(self) -> None:
        png = b"image-bytes"
        html = '<img src="cid:<logo@local>">'
        resolved = resolve_cid_images(
            html, {"logo@local": ("image/png", png)}
        )
        self.assertIn("data:image/png;base64,", resolved)
        self.assertNotIn("cid:", resolved)


if __name__ == "__main__":
    unittest.main()
