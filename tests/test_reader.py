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

    def test_adapt_text_wraps_html_with_text_color_only(self) -> None:
        doc = build_reader_document(
            body_html='<p style="color:#000000">Hello</p>',
            body_plain=None,
            allow_remote=False,
            dark=True,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn('class="post-adapt-text"', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_does_not_wrap_plain_html(self) -> None:
        doc = build_reader_document(
            body_html="<p>Hello</p>",
            body_plain=None,
            allow_remote=False,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

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

    def test_adapt_text_preserves_sender_colors_when_both_set(self) -> None:
        doc = build_reader_document(
            body_html='<div style="color:#000000;background:#ffffff"><p>Hello</p></div>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('style="color:#000000;background:#ffffff"', doc)
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("background: #1e1e1e", doc)
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

    def test_adapt_background_preserves_sender_colors_when_both_set(self) -> None:
        doc = build_reader_document(
            body_html='<div style="color:#000000;background:#fffffe"><p>Hello</p></div>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('style="color:#000000;background:#fffffe"', doc)
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("background: #1e1e1e", doc)
        self.assertNotIn("message-body", doc)

    def test_adapt_skipped_for_bgcolor_and_font_color(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<table bgcolor="#ffffff"><tr><td>'
                '<font color="#000000">Hello</font>'
                "</td></tr></table>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

    def test_adapt_skipped_for_colors_in_style_block(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<style>body { color: #111111; background-color: #fafafa; }</style>"
                "<p>Hello</p>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertNotIn('name="color-scheme" content="light"', doc)

    def test_adapt_text_skipped_for_background_only(self) -> None:
        doc = build_reader_document(
            body_html='<div style="background-color:#ffffff"><p>Hello</p></div>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

    def test_adapt_text_skipped_for_body_text_and_bgcolor_attrs(self) -> None:
        doc = build_reader_document(
            body_html='<body bgcolor="#ffffff" text="#000000"><p>Hello</p></body>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

    def test_adapt_text_applies_for_transparent_background(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div style="color:rgb(0,0,0);background-color:transparent">'
                "<p>Hi Matthias</p></div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_applies_for_style_block_transparent_background(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<style>body { color: #000000; background-color: transparent; }</style>"
                "<p>Hello</p>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_applies_when_only_quoted_history_has_background(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div style="font-size: 12pt; color: rgb(0, 0, 0);">Hi Matthias,</div>'
                '<div id="mail-editor-reference-message-container">'
                '<span style="color: rgb(0, 0, 0); background-color: rgb(255, 255, 255);">'
                "quoted text</span></div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn('class="post-adapt-text"', doc)
        self.assertIn('class="post-painted"', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_adapts_unstyled_sections_in_mixed_message(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#000000">Intro dark text</p>'
                '<p style="color:#666666">Subtitle gray text</p>'
                '<div style="background-color:#1d1d1d;padding:16px">'
                '<p style="color:#ffffff">Bright text in dark box</p></div>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn('class="post-adapt-text"', doc)
        self.assertIn('class="post-painted"', doc)
        self.assertIn("color: inherit !important", doc)
        self.assertIn('name="color-scheme" content="dark"', doc)

    def test_adapt_background_applies_for_mixed_message_with_unstyled_text(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#333333">Intro dark text</p>'
                '<div style="background:#ffffff;color:#000000">'
                "<p>Dark on white box</p></div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('name="color-scheme" content="light"', doc)
        self.assertIn("background: #ffffff", doc)
        self.assertNotIn("message-body", doc)

    def test_adapt_text_preserves_bright_text_outside_painted_regions(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#000000">Dark intro</p>'
                '<p style="color:#ffffff">Bright line</p>'
                '<div style="background-color:#1d1d1d;color:#ffffff">'
                "<p>CTA</p></div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertEqual(doc.count('class="post-adapt-text"'), 2)
        self.assertIn('class="post-painted"', doc)

    def test_adapt_text_preserves_sender_colors_with_background_shorthand(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div style="background:url(https://example.com/bg.png) #ffffff no-repeat">'
                '<p style="color:#000000">Dark on white</p></div>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

    def test_adapt_text_handles_class_based_white_boxes(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<style>.whitebox { background-color: #ffffff; padding: 12px; }</style>"
                '<p style="color:#ffffff">Legible intro on dark shell</p>'
                '<div class="whitebox"><p>Dear Gasometrix Sales Team,</p>'
                "<ul><li>Noble / Inert Gases: Helium (He), Argon (Ar)</li></ul></div>"
                '<p style="color:#ffffff">Legible outro</p>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-painted", doc)
        self.assertIn("whitebox", doc)
        self.assertIn("color:#1e1e1e", doc.replace(" ", ""))
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_skipped_when_new_content_has_background(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div style="color:#000000;background-color:#ffffff">New reply</div>'
                '<div id="geary-quote"><blockquote>'
                '<div style="color:#000000">quoted</div>'
                "</blockquote></div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertNotIn("message-body", doc)
        self.assertNotIn("color: inherit !important", doc)

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
