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

    def test_plain_body_linkifies_www_and_http_urls(self) -> None:
        # Acceptance fixture shaped like today's Gmail "test" message (#193).
        body = "see www.example.com\n\nor http://www.example.com/faq\n"
        doc = build_reader_document(
            body_html=None,
            body_plain=body,
            allow_remote=False,
        )
        self.assertIn(
            '<a href="https://www.example.com">www.example.com</a>',
            doc,
        )
        self.assertIn(
            '<a href="http://www.example.com/faq">'
            "http://www.example.com/faq</a>",
            doc,
        )
        self.assertIn("see ", doc)
        self.assertIn("or ", doc)

    def test_plain_body_linkifies_mailto(self) -> None:
        doc = build_reader_document(
            body_html=None,
            body_plain="Write mailto:contact@example.com please",
            allow_remote=False,
        )
        self.assertIn(
            '<a href="mailto:contact@example.com">mailto:contact@example.com</a>',
            doc,
        )

    def test_html_body_prefers_html_over_plain_linkify(self) -> None:
        doc = build_reader_document(
            body_html="<p>HTML only</p>",
            body_plain="see www.example.com",
            allow_remote=False,
        )
        self.assertIn("<p>HTML only</p>", doc)
        self.assertNotIn("www.example.com", doc)
        self.assertNotIn('<pre class="plain-body">', doc)

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

    def test_adapt_text_keeps_font_color_on_bgcolor_table(self) -> None:
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
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-forced-contrast", doc)
        self.assertIn("post-keep-color", doc)
        self.assertIn('color="#000000"', doc)

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

    def test_adapt_text_applies_for_background_only(self) -> None:
        doc = build_reader_document(
            body_html='<div style="background-color:#ffffff"><p>Hello</p></div>',
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-forced-contrast", doc)
        self.assertIn("color:#1e1e1e", doc.replace(" ", ""))
        self.assertIn("color: inherit !important", doc)

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
        self.assertIn("post-adapt-text", doc)
        self.assertIn("post-painted", doc)
        self.assertIn("post-keep-color", doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_applies_for_outlook_reply_with_signature_before_appendonsend(
        self,
    ) -> None:
        """Outlook reply: new text is color-only; signature has white bg before appendonsend."""
        body_html = (
            '<div class="elementToProof" style="font-size: 12pt; color: rgb(0, 0, 0);">'
            "Sehr geehrter Herr Example</div>"
            '<div class="elementToProof" style="font-size: 12pt; color: rgb(0, 0, 0);">'
            "Besten Dank für die Vollmacht.</div>"
            '<p style="background-color: rgb(255, 255, 255);">'
            '<span style="color: rgb(0, 0, 0);">Marco Colleague</span></p>'
            '<div id="appendonsend"></div>'
            "<hr>"
            '<blockquote><p style="color: rgb(0, 0, 0);">Quoted history</p></blockquote>'
        )
        doc = build_reader_document(
            body_html=body_html,
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-adapt-text", doc)
        self.assertIn("post-painted", doc)
        self.assertIn("post-keep-color", doc)
        self.assertIn("color: inherit !important", doc)

        doc_bg = build_reader_document(
            body_html=body_html,
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        )
        self.assertIn('name="color-scheme" content="light"', doc_bg)
        self.assertIn("background: #ffffff", doc_bg)

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
        self.assertIn("post-painted", doc)
        self.assertIn("post-forced-contrast", doc)
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
        self.assertEqual(doc.count('class="post-adapt-text"'), 1)
        self.assertIn('class="post-keep-color"', doc)
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
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-forced-contrast", doc)
        self.assertIn("post-keep-color", doc)
        self.assertIn("color:#000000", doc.replace(" ", ""))

    def test_adapt_text_dark_shell_on_white_card_without_body_color(self) -> None:
        """Newsletter card: light page + white table, unstyled copy, gray footer (#296)."""
        body_html = (
            "<!doctype html><html><head><style>"
            "@media all { .apple-link a { color: inherit !important; } }"
            "</style></head>"
            '<body style="background-color: #f6f6f6; margin: 0; padding: 0;">'
            '<table class="body" style="width: 100%; background-color: #f6f6f6;">'
            "<tr><td>"
            '<table class="main" style="width: 100%; background: #ffffff; '
            'border-radius: 3px;">'
            "<tr><td><p>Greetings Example User!</p>"
            "<p>Your agent found 1 new listing.</p></td></tr>"
            "</table>"
            '<table class="footer" style="width: 100%;">'
            '<tr><td style="color: #999999; font-size: 12px; text-align: center;">'
            '<span class="apple-link">Example Sender</span></td></tr>'
            "</table>"
            "</td></tr></table></body></html>"
        )
        doc = build_reader_document(
            body_html=body_html,
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-forced-contrast", doc)
        self.assertIn('class="main post-painted post-forced-contrast"', doc)
        self.assertIn("color:#1e1e1e", doc.replace(" ", ""))
        self.assertIn("post-keep-color", doc)
        self.assertIn("color: #999999", doc)
        self.assertIn('name="color-scheme" content="dark"', doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_handles_class_based_white_boxes(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<style>.whitebox { background-color: #ffffff; padding: 12px; }</style>"
                '<p style="color:#ffffff">Legible intro on dark shell</p>'
                '<div class="whitebox"><p>Dear Company Sales Team,</p>'
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

    def test_adapt_text_keeps_sender_color_on_children_of_painted_ancestor(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div class="elementToProof" style="color: black; font-size: 12pt;">'
                "Intro on dark shell</div>"
                '<ul style="background-color: rgb(255, 255, 255);">'
                '<li style="color: black; font-size: 12pt;">'
                '<div class="elementToProof" role="presentation">'
                "<b>Noble / Inert Gases:</b> Helium (He), Argon (Ar)</div>"
                "</li>"
                '<li style="color: black; font-size: 12pt;">'
                '<div class="elementToProof" role="presentation">'
                "<b>Hydrocarbons:</b> Acetylene (C2H2)</div>"
                "</li>"
                "</ul>"
                '<div class="elementToProof" style="background-color: rgb(255, 255, 255); '
                'color: black; font-size: 12pt;">White box paragraph</div>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn("post-keep-color", doc)
        self.assertIn("post-adapt-text", doc)
        self.assertIn('style="color: black', doc)
        self.assertNotIn("color: revert !important", doc)

    def test_adapt_text_keeps_sender_color_on_painted_div_with_inline_color(self) -> None:
        """Outlook pattern: white background and black text on the same div."""
        doc = build_reader_document(
            body_html=(
                '<div class="elementToProof" style="background-color: rgb(255, 255, 255); '
                'font-size: 12pt; color: black;">Dear Company Sales Team,</div>'
                '<div class="elementToProof" style="font-size: 12pt; color: black;">'
                "Body on dark shell</div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn("post-painted post-keep-color", doc)
        self.assertIn('color: black', doc)
        self.assertIn("post-adapt-text", doc)
        self.assertNotIn("color: revert !important", doc)

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

    def test_adapt_text_adapts_forwarded_black_text_on_dark_theme(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div dir="ltr">See below<br></div>'
                '<div class="gmail_quote">'
                '<p style="color:#000000">Original newsletter content</p>'
                "</div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertNotIn("post-quoted-history", doc)
        self.assertIn("post-adapt-text", doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_keeps_readable_grey_on_dark_shell(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<div>Neue Antwort</div>"
                '<blockquote type="cite">'
                '<div style="color:#aaaaaa">Am 22.06.2026 schrieb Adrian:</div>'
                '<div style="color:#cccccc">Hoi Matthias</div>'
                "</blockquote>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertNotIn("post-quoted-history", doc)
        self.assertIn("post-keep-color", doc)

    def test_adapt_text_adapts_unstyled_blockquote_reply_on_dark_theme(self) -> None:
        doc = build_reader_document(
            body_html=(
                "<div>Hoi Matthias</div>"
                '<blockquote type="cite">'
                "<div>Am 22.06.2026 schrieb Example Sender:</div>"
                "<div>Die Boxen sind in Arbeit</div>"
                "</blockquote>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertNotIn("post-quoted-history", doc)
        self.assertIn('name="color-scheme" content="dark"', doc)

    def test_adapt_text_splits_gmail_quote_container(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div>Intro</div>'
                '<div class="gmail_quote gmail_quote_container">'
                '<p style="color:#000000">Quoted</p>'
                "</div>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertNotIn("post-quoted-history", doc)
        self.assertIn("gmail_quote_container", doc)
        self.assertIn("post-adapt-text", doc)

    def test_adapt_text_wraps_blockquote_only_body(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<blockquote type="cite">'
                '<p style="color:#000000">Quoted only</p>'
                "</blockquote>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertNotIn("post-quoted-history", doc)
        self.assertIn("post-adapt-text", doc)

    def test_adapt_text_preserves_reader_link_color_for_plain_mailto(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#000000">Contact '
                '<a href="mailto:contact@example.com">contact@example.com</a></p>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn("a { color: #62a0ea; }", doc)
        self.assertNotRegex(doc, r"<a\b[^>]*post-adapt-text")
        self.assertIn("mailto:contact@example.com", doc)

    def test_adapt_text_adapts_low_contrast_link_color(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<a href="https://example.com" style="color:#000000">'
                "dark link</a>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('class="post-adapt-text"', doc)
        self.assertIn(".message-body .post-adapt-text", doc)

    def test_adapt_text_adapts_table_color_with_unstyled_cells(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<table style="color:#000000">'
                "<tr><td>Dear Sir or Madam,</td></tr>"
                "</table>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('class="post-adapt-text"', doc)
        self.assertIn(".message-body .post-adapt-text", doc)
        self.assertIn("color: inherit !important", doc)

    def test_adapt_text_renders_attachment_placeholder_with_brackets(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#000000">See <IMG_9838.jpg> attached</p>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn('<div class="message-body">', doc)
        self.assertIn(
            '<span class="post-bracketed">&#x3C;IMG_9838.jpg&#x3E;</span>', doc
        )
        self.assertNotIn("<IMG_9838.jpg>", doc)

    def test_adapt_text_preserves_email_address_brackets(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<p style="color:#000000">Am 22.06.2026 schrieb Example Sender '
                "&lt;contact@example.com&gt;:</p>"
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn(
            '<span class="post-bracketed">&#x3C;contact@example.com&#x3E;</span>',
            doc,
        )
        self.assertNotIn("&lt;contact@example.com&gt;", doc)
        self.assertNotIn("<contact@example.com>", doc)

    def test_adapt_text_preserves_brackets_around_mailto_link(self) -> None:
        doc = build_reader_document(
            body_html=(
                '<div style="color:#aaaaaa">Am 22.06.2026 um 14:58 schrieb Example Sender '
                '&lt;<a href="mailto:contact@example.com">contact@example.com</a>&gt;:</div>'
            ),
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIn(
            '<span class="post-bracketed">&#x3C;<a href="mailto:contact@example.com">'
            "contact@example.com</a>&#x3E;</span>",
            doc,
        )
        self.assertNotRegex(doc, r"schrieb Example Sender <a\b")

    def test_accept_sender_renders_attachment_placeholder_with_brackets(self) -> None:
        doc = build_reader_document(
            body_html="<p>See <IMG_9838.jpg> attached</p>",
            body_plain=None,
            allow_remote=False,
            dark=True,
            message_appearance=MESSAGE_APPEARANCE_ACCEPT_SENDER,
        )
        self.assertIn(
            '<span class="post-bracketed">&#x3C;IMG_9838.jpg&#x3E;</span>', doc
        )
        self.assertNotIn("<IMG_9838.jpg>", doc)


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
