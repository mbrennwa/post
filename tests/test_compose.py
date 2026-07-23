# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.compose import (
    ComposeAttachment,
    body_mentions_attachment,
    body_html_for_quoting,
    body_text_for_quoting,
    build_draft_mime_message,
    build_outbound_html_for_compose,
    build_plain_mime_message,
    build_forward_subject,
    build_reply_all_recipients,
    build_reply_references,
    normalize_in_reply_to,
    normalize_references_header,
    parse_references_header,
    validate_compose_mime_fields,
    build_reply_subject,
    extract_reply_address,
    extract_reply_target_addresses,
    format_address_list,
    normalize_email,
    parse_address_header,
    parse_address_list,
    parse_draft_address_list,
    quote_html_forward,
    quote_plain_forward,
    quote_plain_reply,
    read_compose_attachments_from_message,
    split_compose_body_at_quote,
)
from post.mail.helpers import (
    _QuotableHtmlParser,
    _mime_message_raw_bytes,
    extract_attachments,
    extract_message_bodies,
    get_attachment_data,
    html_to_quotable_plain,
    plain_body_looks_truncated,
)

RFC5322_MAX_LINE_LENGTH = 998


def _max_mime_line_length(raw: bytes) -> int:
    """Return the longest line in serialized MIME (RFC 5322 limit is 998)."""
    normalized = raw.replace(b"\r\n", b"\n")
    lines = normalized.split(b"\n")
    return max(len(line) for line in lines) if lines else 0


def _assert_rfc5322_line_lengths(raw: bytes) -> None:
    max_len = _max_mime_line_length(raw)
    if max_len > RFC5322_MAX_LINE_LENGTH:
        raise AssertionError(
            f"MIME line length {max_len} exceeds RFC 5322 limit "
            f"({RFC5322_MAX_LINE_LENGTH})"
        )


class ParseAddressListTests(unittest.TestCase):
    def test_single_address(self) -> None:
        self.assertEqual(parse_address_list("user@example.com"), ["user@example.com"])

    def test_named_address(self) -> None:
        self.assertEqual(
            parse_address_list("Alice <alice@example.com>"),
            ["Alice <alice@example.com>"],
        )

    def test_multiple_addresses(self) -> None:
        self.assertEqual(
            parse_address_list("a@example.com, Bob <b@example.com>"),
            ["a@example.com", "Bob <b@example.com>"],
        )

    def test_empty(self) -> None:
        self.assertEqual(parse_address_list(""), [])

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_address_list("not-an-address")

    def test_missing_local_part_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid"):
            parse_address_list("@xyz")

    def test_missing_domain_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid"):
            parse_address_list("user@")

    def test_email_as_display_name_normalizes_to_bare(self) -> None:
        self.assertEqual(
            parse_address_list("owner@example.com <owner@example.com>"),
            ["owner@example.com"],
        )


class ParseDraftAddressListTests(unittest.TestCase):
    def test_allows_invalid_address(self) -> None:
        self.assertEqual(parse_draft_address_list("asdf"), ["asdf"])

    def test_allows_valid_and_invalid(self) -> None:
        self.assertEqual(
            parse_draft_address_list("asdf, user@example.com"),
            ["asdf", "user@example.com"],
        )

    def test_empty(self) -> None:
        self.assertEqual(parse_draft_address_list(""), [])

    def test_rejects_newlines(self) -> None:
        with self.assertRaises(ValueError):
            parse_draft_address_list("bob@example.com\r\nBcc: evil@example.com")


class BuildReplySubjectTests(unittest.TestCase):
    def test_adds_re_prefix(self) -> None:
        self.assertEqual(build_reply_subject("Hello"), "Re: Hello")

    def test_keeps_existing_re(self) -> None:
        self.assertEqual(build_reply_subject("Re: Hello"), "Re: Hello")


class ExtractReplyAddressTests(unittest.TestCase):
    def test_from_named_address(self) -> None:
        self.assertEqual(
            extract_reply_address("Alice <alice@example.com>"),
            "Alice <alice@example.com>",
        )

    def test_from_bare_address(self) -> None:
        self.assertEqual(
            extract_reply_address("alice@example.com"),
            "alice@example.com",
        )


class ExtractReplyTargetAddressesTests(unittest.TestCase):
    def test_prefers_reply_to_over_from(self) -> None:
        message = {
            "from": "List <list@example.com>",
            "reply_to": "Author <author@example.com>",
        }
        self.assertEqual(
            extract_reply_target_addresses(message),
            ["Author <author@example.com>"],
        )

    def test_multiple_reply_to_addresses(self) -> None:
        message = {
            "from": "List <list@example.com>",
            "reply_to": "Alice <alice@example.com>, Bob <bob@example.com>",
        }
        self.assertEqual(
            extract_reply_target_addresses(message),
            ["Alice <alice@example.com>", "Bob <bob@example.com>"],
        )

    def test_falls_back_to_from_without_reply_to(self) -> None:
        message = {"from": "Alice <alice@example.com>"}
        self.assertEqual(
            extract_reply_target_addresses(message),
            ["Alice <alice@example.com>"],
        )

    def test_raises_without_from_or_reply_to(self) -> None:
        with self.assertRaises(ValueError):
            extract_reply_target_addresses({"from": "", "reply_to": ""})


class QuotePlainReplyTests(unittest.TestCase):
    def test_quotes_body(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        body = quote_plain_reply(original, "Line one\nLine two")
        self.assertIn("On 2026-06-17 16:49:57, Alice <alice@example.com> wrote:", body)
        self.assertIn("> Line one", body)
        self.assertIn("> Line two", body)

    def test_empty_body_placeholder(self) -> None:
        body = quote_plain_reply({"from": "a@b.com", "date_sent": "today"}, None)
        self.assertIn("(no message body)", body)

    def test_quote_uses_from_not_reply_to(self) -> None:
        original = {
            "from": "List <list@example.com>",
            "reply_to": "Author <author@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        body = quote_plain_reply(original, "Hello")
        self.assertIn("On 2026-06-17 16:49:57, List <list@example.com> wrote:", body)
        self.assertNotIn("author@example.com", body.split("wrote:")[0])

    def test_increases_existing_quote_depth(self) -> None:
        original = {"from": "Bob <bob@example.com>", "date_received": "today"}
        body = quote_plain_reply(
            original,
            "Thanks\n\nOn Mon, Alice wrote:\n> Original message",
        )
        self.assertIn(">> Original message", body)
        self.assertNotIn("> > Original message", body)

    def test_increases_multilevel_quote_depth(self) -> None:
        original = {"from": "Carol <carol@example.com>", "date_received": "today"}
        body = quote_plain_reply(original, ">> Already quoted")
        self.assertIn(">>> Already quoted", body)


class BodyTextForQuotingTests(unittest.TestCase):
    def test_prefers_plain_when_complete(self) -> None:
        message = {
            "body_plain": "On Mon, Alice wrote:\n> Hello",
            "body_html": "<blockquote>Hello</blockquote>",
        }
        self.assertEqual(body_text_for_quoting(message), message["body_plain"])

    def test_uses_html_when_plain_is_truncated(self) -> None:
        message = {
            "body_plain": "Thanks for the update",
            "body_html": (
                "<p>Thanks for the update</p>"
                "<blockquote><p>Original message</p></blockquote>"
            ),
        }
        text = body_text_for_quoting(message)
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("Thanks for the update", text)
        self.assertIn("> Original message", text)

    def test_uses_html_when_plain_missing(self) -> None:
        message = {
            "body_plain": None,
            "body_html": "<p>Hello</p><blockquote><p>Nested</p></blockquote>",
        }
        text = body_text_for_quoting(message)
        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("Hello", text)
        self.assertIn("> Nested", text)


class HtmlToQuotablePlainTests(unittest.TestCase):
    def test_blockquote_becomes_quote_lines(self) -> None:
        text = html_to_quotable_plain(
            "<p>Reply text</p><blockquote><p>Quoted body</p></blockquote>"
        )
        self.assertIn("Reply text", text)
        self.assertIn("> Quoted body", text)

    def test_nested_blockquotes_increase_depth(self) -> None:
        text = html_to_quotable_plain(
            "<blockquote><p>Level one</p>"
            "<blockquote><p>Level two</p></blockquote></blockquote>"
        )
        self.assertIn("> Level one", text)
        self.assertIn(">> Level two", text)

    def test_strips_style_and_script_before_visible_text(self) -> None:
        text = html_to_quotable_plain(
            "<html><head><style>/* RESET */ html { color: red; }</style>"
            "<script>alert(1)</script></head>"
            "<body><p>Guten Tag</p></body></html>"
        )
        self.assertIn("Guten Tag", text)
        self.assertNotIn("RESET", text)
        self.assertNotIn("color: red", text)
        self.assertNotIn("alert", text)

    def test_head_meta_and_link_do_not_suppress_body_text(self) -> None:
        text = html_to_quotable_plain(
            "<html><head>"
            '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
            '<link rel="stylesheet" href="https://example.com/email.css">'
            "<style>.x { color: red; }</style>"
            "</head><body><p>Guten Tag, Ihr Abo wird verlängert.</p></body></html>"
        )
        self.assertIn("Guten Tag, Ihr Abo wird verlängert.", text)
        self.assertNotIn("color: red", text)

    def test_fallback_strips_style_and_script_blocks(self) -> None:
        with mock.patch.object(
            _QuotableHtmlParser,
            "feed",
            side_effect=ValueError("force fallback"),
        ):
            text = html_to_quotable_plain(
                "<style>.x { color: red; }</style><p>Hello</p>"
                "<script>console.log('x')</script>"
            )
        self.assertEqual(text, "Hello")

    def test_body_text_for_quoting_ignores_style_blocks(self) -> None:
        message = {
            "body_plain": None,
            "body_html": "<style>.x{color:red}</style><p>Hello</p>",
        }
        self.assertEqual(body_text_for_quoting(message), "Hello")


class PlainBodyLooksTruncatedTests(unittest.TestCase):
    def test_detects_missing_quotes_in_plain(self) -> None:
        self.assertTrue(
            plain_body_looks_truncated(
                "Thanks",
                "<p>Thanks</p><blockquote>Older text</blockquote>",
            )
        )

    def test_plain_with_existing_quotes_is_complete(self) -> None:
        self.assertFalse(
            plain_body_looks_truncated(
                "On Mon, Alice wrote:\n> Hello",
                "<blockquote>Hello</blockquote>",
            )
        )


class BuildForwardSubjectTests(unittest.TestCase):
    def test_adds_fwd_prefix(self) -> None:
        self.assertEqual(build_forward_subject("Hello"), "Fwd: Hello")

    def test_keeps_existing_fwd(self) -> None:
        self.assertEqual(build_forward_subject("Fwd: Hello"), "Fwd: Hello")

    def test_keeps_existing_fw(self) -> None:
        self.assertEqual(build_forward_subject("FW: Hello"), "FW: Hello")


class BuildDraftMimeMessageTests(unittest.TestCase):
    def test_allows_empty_to_and_subject(self) -> None:
        message = build_draft_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=[],
            cc=None,
            bcc=None,
            subject="",
            body="Work in progress",
        )
        self.assertEqual(message.get_subject(), "")
        self.assertIsNotNone(message.get_from())

    def test_includes_recipients_when_present(self) -> None:
        message = build_draft_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=["cc@example.com"],
            bcc=None,
            subject="Hi",
            body="Hello",
        )
        self.assertIsNotNone(message.get_recipients("to"))
        self.assertIsNotNone(message.get_recipients("cc"))

    def test_allows_unparseable_to_address(self) -> None:
        message = build_draft_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["asdf"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
        )
        self.assertIsNotNone(message.get_recipients("to"))


class BuildPlainMimeMessageTests(unittest.TestCase):
    def test_empty_body_has_valid_content_wrapper(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="",
        )
        self.assertIsNotNone(message.get_content())

    def test_without_attachments_is_single_part(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
        )
        content_type = message.get_content_type()
        self.assertIsNotNone(content_type)
        self.assertEqual(content_type.simple(), "text/plain")

    def test_with_attachments_is_multipart_mixed(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Files",
            body="See attached",
            attachments=[
                ComposeAttachment(
                    filename="doc.pdf",
                    mime_type="application/pdf",
                    data=b"%PDF-fake",
                )
            ],
        )
        content_type = message.get_content_type()
        self.assertIsNotNone(content_type)
        self.assertEqual(content_type.simple(), "multipart/mixed")
        extracted = extract_attachments(message)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["filename"], "doc.pdf")
        filename, data = get_attachment_data(message, 0)
        self.assertEqual(filename, "doc.pdf")
        self.assertEqual(data, b"%PDF-fake")

    def test_attachments_serialize_with_base64_encoding(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Binary",
            body="2",
            attachments=[
                ComposeAttachment(
                    filename="Untitled.jpg",
                    mime_type="image/jpeg",
                    data=b"\xff\xd8\xff\xe0" + b"\x00" * 16,
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"Content-Transfer-Encoding: base64", raw)
        self.assertNotIn(b"\xff\xd8", raw)

    def test_multipart_non_ascii_body_uses_quoted_printable_cte(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
            attachments=[
                ComposeAttachment(
                    filename="note.txt",
                    mime_type="text/plain",
                    data=b"attachment",
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertRegex(
            raw,
            rb"Content-Type: text/plain[^\n]*\nContent-Transfer-Encoding: quoted-printable",
        )
        self.assertIn(b"Content-Transfer-Encoding: base64", raw)
        self.assertNotRegex(
            raw,
            rb"Content-Type: text/plain[^\n]*\nContent-Transfer-Encoding: 8bit",
        )

    def test_multipart_ascii_body_uses_quoted_printable_cte(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="ASCII",
            body="Hello",
            attachments=[
                ComposeAttachment(
                    filename="note.txt",
                    mime_type="text/plain",
                    data=b"attachment",
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertRegex(
            raw,
            rb"Content-Type: text/plain[^\n]*\nContent-Transfer-Encoding: quoted-printable",
        )

    def test_alternative_non_ascii_body_uses_quoted_printable_cte(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode HTML",
            body="Café",
            body_html="<p>Café</p>",
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertEqual(raw.count(b"Content-Transfer-Encoding: quoted-printable"), 2)
        self.assertNotIn(b"Content-Transfer-Encoding: 8bit", raw)

    def test_with_multiple_attachments_round_trips(self) -> None:
        attachments = [
            ComposeAttachment(
                filename="one.txt",
                mime_type="text/plain",
                data=b"first",
            ),
            ComposeAttachment(
                filename="two.bin",
                mime_type="application/octet-stream",
                data=b"\x00\x01",
            ),
        ]
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Multi",
            body="Body",
            attachments=attachments,
        )
        round_tripped = read_compose_attachments_from_message(message)
        self.assertEqual(len(round_tripped), 2)
        self.assertEqual(round_tripped[0].filename, "one.txt")
        self.assertEqual(round_tripped[0].data, b"first")
        self.assertEqual(round_tripped[1].filename, "two.bin")
        self.assertEqual(round_tripped[1].data, b"\x00\x01")

    def test_non_ascii_attachment_filename_uses_utf8_rfc5987(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode file",
            body="See attached",
            attachments=[
                ComposeAttachment(
                    filename="résumé.pdf",
                    mime_type="application/pdf",
                    data=b"%PDF-fake",
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(
            b"Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf",
            raw,
        )
        self.assertNotIn(b"ISO-8859-1", raw)
        filename, data = get_attachment_data(message, 0)
        self.assertEqual(filename, "résumé.pdf")
        self.assertEqual(data, b"%PDF-fake")

    def test_ascii_attachment_filename_unchanged(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="ASCII file",
            body="See attached",
            attachments=[
                ComposeAttachment(
                    filename="doc.pdf",
                    mime_type="application/pdf",
                    data=b"%PDF-fake",
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b'Content-Disposition: attachment; filename="doc.pdf"', raw)
        self.assertNotIn(b"filename*=", raw)

    def test_build_plain_mime_message_unicode_attachment_filename(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode attachment",
            body="See attached",
            attachments=[
                ComposeAttachment(
                    filename="Grüße.txt",
                    mime_type="text/plain",
                    data=b"hello",
                )
            ],
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(
            b"Content-Disposition: attachment; filename*=utf-8''Gr%C3%BC%C3%9Fe.txt",
            raw,
        )


class HeaderInjectionTests(unittest.TestCase):
    _BASE = {
        "from_address": "alice@example.com",
        "to": ["bob@example.com"],
        "cc": None,
        "bcc": None,
        "body": "Hello",
    }
    _INJECT = "\r\nBcc: evil@example.com"

    def _assert_rejects_plain(self, **overrides: object) -> None:
        kwargs = {**self._BASE, "from_name": "Alice", "subject": "Hi", **overrides}
        with self.assertRaises(ValueError):
            build_plain_mime_message(**kwargs)

    def _assert_rejects_draft(self, **overrides: object) -> None:
        kwargs = {
            **self._BASE,
            "from_name": "Alice",
            "subject": "Hi",
            "to": None,
            **overrides,
        }
        with self.assertRaises(ValueError):
            build_draft_mime_message(**kwargs)

    def test_rejects_subject_injection_plain(self) -> None:
        self._assert_rejects_plain(subject=f"Hi{self._INJECT}")

    def test_rejects_subject_injection_draft(self) -> None:
        self._assert_rejects_draft(subject=f"Hi{self._INJECT}")

    def test_rejects_from_name_injection_plain(self) -> None:
        self._assert_rejects_plain(from_name=f"Alice{self._INJECT}")

    def test_rejects_from_name_injection_draft(self) -> None:
        self._assert_rejects_draft(from_name=f"Alice{self._INJECT}")

    def test_rejects_in_reply_to_injection_plain(self) -> None:
        self._assert_rejects_plain(in_reply_to=f"<orig@example.com>{self._INJECT}")

    def test_rejects_in_reply_to_injection_draft(self) -> None:
        self._assert_rejects_draft(in_reply_to=f"<orig@example.com>{self._INJECT}")

    def test_rejects_to_display_name_injection(self) -> None:
        with self.assertRaises(ValueError):
            build_plain_mime_message(
                from_address="alice@example.com",
                from_name="Alice",
                subject="Hi",
                body="Hello",
                cc=None,
                bcc=None,
                to=[f"Evil{self._INJECT} <bob@example.com>"],
            )

    def test_rejects_bare_to_address_injection(self) -> None:
        with self.assertRaises(ValueError):
            build_plain_mime_message(
                from_address="alice@example.com",
                from_name="Alice",
                subject="Hi",
                body="Hello",
                cc=None,
                bcc=None,
                to=[f"bob@example.com{self._INJECT}"],
            )

    def test_parse_address_list_rejects_newline_in_bare_address(self) -> None:
        with self.assertRaises(ValueError):
            parse_address_list("bob@example.com\r\nBcc: evil@example.com")

    def test_rejects_attachment_filename_injection(self) -> None:
        with self.assertRaises(ValueError):
            build_plain_mime_message(
                **self._BASE,
                from_name="Alice",
                subject="Hi",
                attachments=[
                    ComposeAttachment(
                        filename=f"doc.pdf{self._INJECT}",
                        mime_type="application/pdf",
                        data=b"x",
                    )
                ],
            )

    def test_clean_message_serializes_without_extra_headers(self) -> None:
        message = build_plain_mime_message(
            **self._BASE,
            from_name="Alice",
            subject="Hi",
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        header_block = raw.split(b"\r\n\r\n", 1)[0]
        self.assertNotIn(b"Bcc: evil@example.com", header_block)


class BuildDraftMimeMessageAttachmentTests(unittest.TestCase):
    def test_draft_with_attachments(self) -> None:
        message = build_draft_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=None,
            cc=None,
            bcc=None,
            subject="Draft",
            body="Draft body",
            attachments=[
                ComposeAttachment(
                    filename="note.txt",
                    mime_type="text/plain",
                    data=b"attachment text",
                )
            ],
        )
        content_type = message.get_content_type()
        self.assertIsNotNone(content_type)
        self.assertEqual(content_type.simple(), "multipart/mixed")
        round_tripped = read_compose_attachments_from_message(message)
        self.assertEqual(len(round_tripped), 1)
        self.assertEqual(round_tripped[0].filename, "note.txt")


class QuotePlainForwardTests(unittest.TestCase):
    def test_includes_headers_and_body(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        body = quote_plain_forward(original, "Hello there")
        self.assertIn("---------- Forwarded message ---------", body)
        self.assertIn("From: Alice <alice@example.com>", body)
        self.assertIn("Hello there", body)

    def test_omits_bcc_from_quoted_header(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "bcc": "Dave <dave@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        body = quote_plain_forward(original, "Hello there")
        self.assertNotIn("Bcc:", body)
        self.assertNotIn("dave@example.com", body)


class BuildReplyAllRecipientsTests(unittest.TestCase):
    def test_includes_sender_and_other_recipients(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>, Me <me@example.com>",
            "cc": "Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("me@example.com")},
        )
        self.assertEqual(
            to_addrs,
            ["Alice <alice@example.com>", "Bob <bob@example.com>"],
        )
        self.assertEqual(cc_addrs, ["Carol <carol@example.com>"])

    def test_deduplicates_addresses(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Alice <alice@example.com>, Bob <bob@example.com>",
            "cc": "Bob <bob@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses=set(),
        )
        self.assertEqual(to_addrs, ["Alice <alice@example.com>", "Bob <bob@example.com>"])
        self.assertEqual(cc_addrs, [])

    def test_excludes_all_own_addresses(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Me <me@example.com>",
            "cc": "Also Me <also@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={
                normalize_email("me@example.com"),
                normalize_email("also@example.com"),
            },
        )
        self.assertEqual(to_addrs, ["Alice <alice@example.com>"])
        self.assertEqual(cc_addrs, [])

    def test_reply_all_includes_cc_not_in_from_or_to(self) -> None:
        original = {
            "from": "Test Sender <sender@example.com>",
            "to": "owner@local, owner@example.com",
            "cc": "Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={
                normalize_email("sender@example.com"),
                normalize_email("owner@local"),
            },
        )
        self.assertEqual(
            to_addrs,
            ["owner@example.com"],
        )
        self.assertEqual(cc_addrs, ["Carol <carol@example.com>"])

    def test_reply_all_omits_cc_matching_compose_from(self) -> None:
        original = {
            "from": "Test Sender <sender@example.com>",
            "to": "owner@local, owner@example.com, Coworker <coworker@example.com>",
            "cc": "sender@example.com",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={
                normalize_email("sender@example.com"),
                normalize_email("owner@local"),
            },
        )
        self.assertEqual(
            to_addrs,
            ["owner@example.com", "Coworker <coworker@example.com>"],
        )
        self.assertEqual(cc_addrs, [])

    def test_reply_all_omits_cc_addresses_already_in_to(self) -> None:
        original = {
            "from": "Test Sender <sender@example.com>",
            "to": "owner@example.com",
            "cc": "sender@example.com",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("owner@example.com")},
        )
        self.assertEqual(
            to_addrs,
            ["Test Sender <sender@example.com>"],
        )
        self.assertEqual(cc_addrs, [])

    def test_self_to_self_falls_back_to_from(self) -> None:
        original = {
            "from": "Me <me@example.com>",
            "to": "Me <me@example.com>",
            "cc": "",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("me@example.com")},
        )
        self.assertEqual(to_addrs, ["Me <me@example.com>"])
        self.assertEqual(cc_addrs, [])

    def test_reply_all_uses_reply_to_and_all_original_to(self) -> None:
        original = {
            "from": "List <newsletters@list.example.com>",
            "reply_to": "Author <author@example.com>",
            "to": (
                "List <newsletters@list.example.com>, "
                "Alice <alice@example.com>, "
                "Me <me@example.com>"
            ),
            "cc": "Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("me@example.com")},
        )
        self.assertEqual(
            to_addrs,
            [
                "Author <author@example.com>",
                "List <newsletters@list.example.com>",
                "Alice <alice@example.com>",
            ],
        )
        self.assertEqual(cc_addrs, ["Carol <carol@example.com>"])

    def test_reply_all_multiple_reply_to_addresses(self) -> None:
        original = {
            "from": "List <list@example.com>",
            "reply_to": "Alice <alice@example.com>, Bob <bob@example.com>",
            "to": "Me <me@example.com>",
            "cc": "",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("me@example.com")},
        )
        self.assertEqual(
            to_addrs,
            ["Alice <alice@example.com>", "Bob <bob@example.com>"],
        )
        self.assertEqual(cc_addrs, [])

    def test_no_recipients_raises_without_from(self) -> None:
        original = {
            "from": "",
            "to": "Me <me@example.com>",
            "cc": "",
        }
        with self.assertRaises(ValueError):
            build_reply_all_recipients(
                original,
                own_addresses={normalize_email("me@example.com")},
            )


class ParseAddressHeaderTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(parse_address_header(""), [])

    def test_multiple(self) -> None:
        self.assertEqual(
            parse_address_header("a@example.com, Bob <b@example.com>"),
            ["a@example.com", "Bob <b@example.com>"],
        )

    def test_skips_invalid_address(self) -> None:
        self.assertEqual(parse_address_header("not-an-address"), [])

    def test_skips_missing_local_part(self) -> None:
        self.assertEqual(parse_address_header("@xyz"), [])

    def test_skips_missing_domain(self) -> None:
        self.assertEqual(parse_address_header("user@"), [])

    def test_keeps_valid_and_skips_invalid_in_list(self) -> None:
        self.assertEqual(
            parse_address_header("a@example.com, not-an-address, @invalid"),
            ["a@example.com"],
        )

    def test_email_as_display_name_normalizes_to_bare(self) -> None:
        self.assertEqual(
            parse_address_header("owner@example.com <owner@example.com>"),
            ["owner@example.com"],
        )

    def test_valid_entries_match_strict_parser(self) -> None:
        cases = [
            "user@example.com",
            "Alice <alice@example.com>",
            "a@example.com, Bob <b@example.com>",
            "owner@example.com <owner@example.com>",
            '"Last, First" <person@example.com>',
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    parse_address_header(case),
                    parse_address_list(case),
                )

    def test_round_trip_through_compose_field_text(self) -> None:
        for header in (
            '"Last, First" <person@example.com>',
            "Alice <alice@example.com>, Bob <bob@example.com>",
        ):
            with self.subTest(header=header):
                field_text = format_address_list(parse_address_header(header))
                self.assertEqual(parse_address_list(field_text), parse_address_header(header))


class BuildReplyAllMalformedHeaderTests(unittest.TestCase):
    def test_skips_invalid_stored_addresses(self) -> None:
        original = {
            "from": "@bad-from",
            "reply_to": "",
            "to": "Alice <alice@example.com>, @invalid",
            "cc": "user@, Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses=set(),
        )
        self.assertEqual(
            to_addrs,
            ["Alice <alice@example.com>"],
        )
        self.assertEqual(cc_addrs, ["Carol <carol@example.com>"])

    def test_prefilled_addresses_pass_send_validation(self) -> None:
        original = {
            "from": "Author <author@example.com>",
            "reply_to": "",
            "to": "Alice <alice@example.com>, @invalid",
            "cc": "user@, Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses=set(),
        )
        parse_address_list(format_address_list(to_addrs))
        parse_address_list(format_address_list(cc_addrs))

    def test_reply_prefill_with_comma_display_name_passes_send_validation(self) -> None:
        to_addrs = extract_reply_target_addresses(
            {"from": '"Last, First" <person@example.com>'}
        )
        parse_address_list(format_address_list(to_addrs))


class ExtractReplyAddressInvalidFromTests(unittest.TestCase):
    def test_raises_for_unparseable_from(self) -> None:
        with self.assertRaisesRegex(ValueError, "no From address"):
            extract_reply_address("@bad-from")


class FormatAddressListTests(unittest.TestCase):
    def test_joins_addresses(self) -> None:
        self.assertEqual(
            format_address_list(["a@example.com", "Bob <b@example.com>"]),
            "a@example.com, Bob <b@example.com>",
        )


class NormalizeInReplyToTests(unittest.TestCase):
    def test_none_and_empty(self) -> None:
        self.assertIsNone(normalize_in_reply_to(None))
        self.assertIsNone(normalize_in_reply_to(""))
        self.assertIsNone(normalize_in_reply_to("   "))

    def test_bare_numeric(self) -> None:
        self.assertEqual(
            normalize_in_reply_to("18137428606209368569"),
            "<18137428606209368569>",
        )

    def test_already_bracketed(self) -> None:
        self.assertEqual(
            normalize_in_reply_to("<parent@example.com>"),
            "<parent@example.com>",
        )

    def test_bare_addr_spec(self) -> None:
        self.assertEqual(
            normalize_in_reply_to("parent@example.com"),
            "<parent@example.com>",
        )


class BuildReplyReferencesTests(unittest.TestCase):
    def test_message_id_only(self) -> None:
        self.assertEqual(
            build_reply_references("<abc@example.com>"),
            "<abc@example.com>",
        )

    def test_appends_to_existing(self) -> None:
        self.assertEqual(
            build_reply_references(
                "<new@example.com>",
                "<old@example.com>",
            ),
            "<old@example.com> <new@example.com>",
        )

    def test_substring_false_positive_appends(self) -> None:
        self.assertEqual(
            build_reply_references(
                "<a@b.c>",
                "<xa@b.c@other.com>",
            ),
            "<xa@b.c@other.com> <a@b.c>",
        )

    def test_skips_duplicate_token(self) -> None:
        self.assertEqual(
            build_reply_references(
                "<abc@example.com>",
                "<abc@example.com> <def@example.com>",
            ),
            "<abc@example.com> <def@example.com>",
        )

    def test_bracket_mismatch_treated_as_duplicate(self) -> None:
        self.assertEqual(
            build_reply_references(
                "abc@example.com",
                "<abc@example.com>",
            ),
            "<abc@example.com>",
        )

    def test_normalizes_bare_references_on_append(self) -> None:
        self.assertEqual(
            build_reply_references(
                "<new@example.com>",
                "abc@example.com",
            ),
            "<abc@example.com> <new@example.com>",
        )

    def test_prunes_long_chain(self) -> None:
        chain = " ".join(f"<m{i}@example.com>" for i in range(60))
        result = build_reply_references("<m60@example.com>", chain)
        ids = parse_references_header(result)
        self.assertEqual(len(ids), 50)
        self.assertEqual(ids[0], "<m0@example.com>")
        self.assertEqual(ids[-1], "<m60@example.com>")

    def test_prunes_references_without_new_message_id(self) -> None:
        chain = " ".join(f"<m{i}@example.com>" for i in range(60))
        result = build_reply_references(None, chain)
        ids = parse_references_header(result)
        self.assertEqual(len(ids), 50)
        self.assertEqual(ids[0], "<m0@example.com>")
        self.assertEqual(ids[-1], "<m59@example.com>")


class ReferencesNormalizationTests(unittest.TestCase):
    def test_unfolds_folded_references_between_ids(self) -> None:
        folded = "<a@x.com> <b@x.com>\r\n <c@x.com>"
        self.assertEqual(
            normalize_references_header(folded),
            "<a@x.com> <b@x.com> <c@x.com>",
        )

    def test_strips_embedded_newline_in_message_id_token(self) -> None:
        broken = "<foo\r\n@bar.com> <baz@x.com>"
        self.assertEqual(
            normalize_references_header(broken),
            "<foo@bar.com> <baz@x.com>",
        )

    def test_folded_references_passes_compose_validation(self) -> None:
        folded = "<a@x.com> <b@x.com>\r\n <c@x.com>"
        normalized = normalize_references_header(folded)
        validate_compose_mime_fields(
            from_name=None,
            subject="Thread",
            references=normalized,
        )

    def test_validate_accepts_raw_folded_references(self) -> None:
        """Outbox enqueue must normalize before sanitize (#152)."""
        folded = "<a@x.com> <b@x.com>\r\n <c@x.com>"
        validate_compose_mime_fields(
            from_name=None,
            subject="Thread",
            references=folded,
        )

    def test_validate_rejects_in_reply_to_bare_newline_injection(self) -> None:
        with self.assertRaises(ValueError):
            validate_compose_mime_fields(
                from_name=None,
                subject="Thread",
                in_reply_to="<orig@example.com>\r\nBcc: evil@example.com",
            )

    def test_validate_accepts_folded_in_reply_to(self) -> None:
        validate_compose_mime_fields(
            from_name=None,
            subject="Thread",
            in_reply_to="<parent@example.com>\r\n <ignored-fold>",
        )

    def test_build_reply_references_cleans_folded_input(self) -> None:
        folded = "<a@x.com> <b@x.com>\r\n <c@x.com>"
        result = build_reply_references("<new@x.com>", folded)
        validate_compose_mime_fields(
            from_name=None,
            subject="Thread",
            in_reply_to="<new@x.com>",
            references=result,
        )
        self.assertNotIn("\n", result or "")
        self.assertNotIn("\r", result or "")

    def test_normalize_in_reply_to_strips_embedded_newlines(self) -> None:
        normalized = normalize_in_reply_to("parent\r\n@example.com")
        self.assertEqual(normalized, "<parent@example.com>")
        validate_compose_mime_fields(
            from_name=None,
            subject="Thread",
            in_reply_to=normalized,
        )

    def test_injection_in_subject_still_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Subject must not contain line breaks.",
        ):
            validate_compose_mime_fields(
                from_name=None,
                subject="Hello\r\nBcc: evil@example.com",
            )


class SignatureComposeTests(unittest.TestCase):
    def test_format_signature_block(self) -> None:
        from post.mail.compose import compose_body_with_signature, format_signature_block

        self.assertEqual(format_signature_block(""), "")
        self.assertEqual(format_signature_block(" "), "")
        self.assertEqual(format_signature_block("\u200b"), "")
        self.assertEqual(
            format_signature_block("Alice\nExample Corp"),
            "Alice\nExample Corp",
        )

    def test_body_is_unedited_signature_template(self) -> None:
        from post.mail.compose import body_is_unedited_signature_template

        signatures = ["Alice"]
        self.assertTrue(body_is_unedited_signature_template("\n\nAlice", signatures))
        self.assertFalse(
            body_is_unedited_signature_template("Hello\n\nAlice", signatures)
        )

    def test_body_should_follow_account_signature_after_account_switch(self) -> None:
        from post.mail.compose import (
            body_should_follow_account_signature,
            compose_body_with_signature,
        )

        signature_a = "Alice\nExample Corp"
        body_a = compose_body_with_signature(
            mode="new",
            quoted_body="",
            signature=signature_a,
        )
        self.assertTrue(
            body_should_follow_account_signature(
                body_a,
                known_signatures=[signature_a],
                tracked_account_signature=signature_a,
            )
        )
        self.assertTrue(
            body_should_follow_account_signature(
                "\n\n",
                known_signatures=[signature_a],
                tracked_account_signature=signature_a,
            )
        )
        self.assertFalse(
            body_should_follow_account_signature(
                "Hello there",
                known_signatures=[signature_a],
                tracked_account_signature=signature_a,
            )
        )

    def test_new_message_body(self) -> None:
        from post.mail.compose import (
            append_new_message_signature_if_needed,
            compose_body_with_signature,
            finalize_body_after_signature_sync,
            find_auto_signature_offset,
            merge_user_body_with_signature,
            replace_new_message_signature,
            sync_new_message_body_signature,
        )

        self.assertEqual(
            compose_body_with_signature(
                mode="new",
                quoted_body="",
                signature="Alice",
            ),
            "\n\nAlice",
        )
        self.assertEqual(
            compose_body_with_signature(mode="new", quoted_body="", signature=""),
            "",
        )
        self.assertEqual(
            merge_user_body_with_signature("Hello", "Bob"),
            "Hello\n\nBob",
        )
        self.assertEqual(
            merge_user_body_with_signature("", "Alice"),
            "\n\nAlice",
        )
        self.assertEqual(merge_user_body_with_signature("", ""), "")
        self.assertEqual(merge_user_body_with_signature("Hello", ""), "Hello")

        signature_a = "Alice\nExample Corp"
        body_a = compose_body_with_signature(
            mode="new",
            quoted_body="",
            signature=signature_a,
        )
        self.assertEqual(
            find_auto_signature_offset(
                body_a,
                tracked_signature=signature_a,
                known_signatures=[signature_a],
            ),
            0,
        )
        self.assertEqual(
            find_auto_signature_offset(
                f"Hello\n\n{signature_a}",
                tracked_signature=signature_a,
                known_signatures=[signature_a],
            ),
            5,
        )
        self.assertEqual(
            find_auto_signature_offset(
                f"\n\n{signature_a}",
                tracked_signature=signature_a,
                known_signatures=[signature_a],
            ),
            0,
        )
        cleared = sync_new_message_body_signature(
            body_a,
            tracked_signature=signature_a,
            new_signature="",
            known_signatures=[signature_a],
        )
        self.assertEqual(cleared, ("", None))
        self.assertEqual(
            sync_new_message_body_signature(
                "\n\n",
                tracked_signature=signature_a,
                new_signature="",
                known_signatures=[signature_a],
            ),
            ("", None),
        )
        self.assertEqual(
            sync_new_message_body_signature(
                "Hello there",
                tracked_signature=signature_a,
                new_signature="Bob",
                known_signatures=[signature_a],
            ),
            None,
        )
        self.assertEqual(
            sync_new_message_body_signature(
                f"Hello\n\n{signature_a}",
                tracked_signature=signature_a,
                new_signature="Bob",
                known_signatures=[signature_a],
            ),
            ("Hello\n\nBob", "Bob"),
        )
        self.assertEqual(
            sync_new_message_body_signature(
                f"Hello\n\n{signature_a}\n",
                tracked_signature=signature_a,
                new_signature="",
                known_signatures=[signature_a],
            ),
            ("Hello", None),
        )
        self.assertEqual(
            replace_new_message_signature(
                f"Hello\n\n{signature_a}\n",
                new_signature="Bob",
                tracked_signature=None,
                previous_signature=signature_a,
                known_signatures=[signature_a],
            ),
            ("Hello\n\nBob", "Bob"),
        )
        self.assertEqual(
            append_new_message_signature_if_needed(
                "Hello there",
                new_signature="Bob",
                known_signatures=[],
            ),
            ("Hello there\n\nBob", "Bob"),
        )
        self.assertEqual(finalize_body_after_signature_sync("\n\n", ""), "")
        self.assertEqual(finalize_body_after_signature_sync("Hello", ""), "Hello")
        self.assertEqual(
            replace_new_message_signature(
                "Hello there",
                new_signature="Bob",
                tracked_signature=None,
                previous_signature="",
                known_signatures=[],
            ),
            ("Hello there\n\nBob", "Bob"),
        )

    def test_reply_inserts_signature_before_quote(self) -> None:
        from post.mail.compose import compose_body_with_signature

        quoted = "\n\nOn today, a@b.com wrote:\n> hi\n"
        body = compose_body_with_signature(
            mode="reply",
            quoted_body=quoted,
            signature="Alice",
        )
        self.assertTrue(body.startswith("\n\nAlice\n\n"))
        self.assertTrue(body.endswith("> hi\n"))

    def test_forward_keeps_quote_without_signature(self) -> None:
        from post.mail.compose import compose_body_with_signature

        quoted = "---------- Forwarded message ---------\nHello"
        self.assertEqual(
            compose_body_with_signature(
                mode="forward",
                quoted_body=quoted,
                signature="Alice",
            ),
            quoted,
        )


class HtmlForwardReplyTests(unittest.TestCase):
    def test_body_html_for_quoting_returns_original_html(self) -> None:
        message = {"body_html": "<p><b>Hello</b></p>", "body_plain": "Hello"}
        self.assertEqual(body_html_for_quoting(message), "<p><b>Hello</b></p>")

    def test_quote_html_forward_preserves_source_html(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        source = '<p style="color:#000000">Newsletter</p>'
        quoted = quote_html_forward(original, source)
        self.assertIn('class="post_quote"', quoted)
        self.assertIn(source, quoted)
        self.assertIn("---------- Forwarded message ---------", quoted)

    def test_quote_html_forward_omits_bcc_from_header(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "bcc": "Dave <dave@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        quoted = quote_html_forward(original, "<p>Newsletter</p>")
        self.assertNotIn("Bcc:", quoted)
        self.assertNotIn("dave@example.com", quoted)

    def test_build_outbound_html_for_forward_omits_bcc_in_quote(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "bcc": "Dave <dave@example.com>",
            "date_received": "2026-06-17 16:49:57",
            "body_html": "<p>Newsletter</p>",
        }
        quoted_plain = quote_plain_forward(original, "Newsletter")
        html = build_outbound_html_for_compose(
            body_plain=f"See below{quoted_plain}",
            mode="forward",
            reply_to=original,
            quoted_html_source=original["body_html"],
            quoted_plain_expected=quoted_plain,
        )
        assert html is not None
        self.assertNotIn("Bcc:", html)
        self.assertNotIn("dave@example.com", html)

    def test_build_plain_mime_message_with_html_is_multipart_alternative(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Fwd: Hi",
            body="See below\n",
            body_html='<blockquote class="post_quote"><p>Hi</p></blockquote>',
        )
        content_type = message.get_content_type()
        self.assertIsNotNone(content_type)
        self.assertEqual(content_type.simple(), "multipart/alternative")

    def test_build_plain_mime_message_with_html_and_attachment(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Files",
            body="See attached",
            body_html="<p>See attached</p>",
            attachments=[
                ComposeAttachment(
                    filename="doc.pdf",
                    mime_type="application/pdf",
                    data=b"%PDF-fake",
                )
            ],
        )
        content_type = message.get_content_type()
        self.assertIsNotNone(content_type)
        self.assertEqual(content_type.simple(), "multipart/mixed")
        extracted = extract_attachments(message)
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["filename"], "doc.pdf")

    def test_build_plain_mime_message_omits_bcc_when_requested(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=["secret@example.com"],
            subject="Hi",
            body="Hello",
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertNotIn(b"Bcc:", raw)

    def test_build_plain_mime_message_includes_bcc_by_default(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=["secret@example.com"],
            subject="Hi",
            body="Hello",
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"Bcc:", raw)

    def test_unchanged_plain_quote_uses_original_html(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "date_received": "2026-06-17",
        }
        source = '<p style="color:#000000">Original</p>'
        quoted_plain = quote_plain_forward(original, "Plain fallback")
        body_plain = f"My note{quoted_plain}"
        html = build_outbound_html_for_compose(
            body_plain=body_plain,
            mode="forward",
            reply_to=original,
            quoted_html_source=source,
            quoted_plain_expected=quoted_plain,
        )
        self.assertIsNotNone(html)
        assert html is not None
        self.assertIn(source, html)
        self.assertIn("My note", html)

    def test_edited_plain_quote_falls_back_to_escaped_plain(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "date_received": "2026-06-17",
        }
        source = "<p>Original</p>"
        quoted_plain = quote_plain_forward(original, "Plain fallback")
        user_plain, quoted_part = split_compose_body_at_quote(
            f"Edited intro{quoted_plain}edited quote",
            "forward",
        )
        self.assertEqual(user_plain, "Edited intro")
        html = build_outbound_html_for_compose(
            body_plain=f"{user_plain}{quoted_part}",
            mode="forward",
            reply_to=original,
            quoted_html_source=source,
            quoted_plain_expected=quoted_plain,
        )
        self.assertIsNotNone(html)
        assert html is not None
        self.assertNotIn(source, html)
        self.assertIn("edited quote", html)


class BodyMentionsAttachmentTests(unittest.TestCase):
    def test_detects_english_keywords(self) -> None:
        self.assertTrue(body_mentions_attachment("Please see the attachment"))
        self.assertTrue(body_mentions_attachment("files attached"))
        self.assertTrue(body_mentions_attachment("Document enclosed"))

    def test_detects_german_keywords(self) -> None:
        self.assertTrue(body_mentions_attachment("im Anhang finden Sie die Rechnung"))
        self.assertTrue(body_mentions_attachment("siehe Anhänge"))

    def test_no_match_without_keywords(self) -> None:
        self.assertFalse(body_mentions_attachment("Hello, no files here"))
        self.assertFalse(body_mentions_attachment(""))

    def test_excludes_bare_attach(self) -> None:
        self.assertFalse(body_mentions_attachment("please attach"))

    def test_ignores_quoted_reply_text(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        quoted = quote_plain_reply(original, "Please see the attachment")
        body = f"Thanks{quoted}"
        self.assertFalse(body_mentions_attachment(body, mode="reply"))

    def test_detects_user_text_in_reply(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "date_received": "2026-06-17 16:49:57",
        }
        quoted = quote_plain_reply(original, "Plain body")
        body = f"See the attachment{quoted}"
        self.assertTrue(body_mentions_attachment(body, mode="reply"))

    def test_forward_ignores_quoted_attachment_mention(self) -> None:
        original = {
            "from": "Alice <alice@example.com>",
            "to": "Bob <bob@example.com>",
            "date_received": "2026-06-17",
        }
        quoted = quote_plain_forward(original, "Please see the attachment")
        body = f"FYI{quoted}"
        self.assertFalse(body_mentions_attachment(body, mode="forward"))


class OutboundMimeParityTests(unittest.TestCase):
    def test_build_plain_mime_message_includes_message_id_and_date(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"Message-ID:", raw)
        self.assertIn(b"Date:", raw)

    def test_build_plain_mime_message_thread_headers_and_omits_bcc_on_wire(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=["carol@example.com"],
            bcc=["secret@example.com"],
            subject="Thread test",
            body="Hello",
            in_reply_to="<parent@example.com>",
            references="<parent@example.com>",
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"In-Reply-To: <parent@example.com>", raw)
        self.assertIn(b"References: <parent@example.com>", raw)
        self.assertNotIn(b"Bcc:", raw)

    def test_build_plain_mime_message_normalizes_folded_references(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Thread test",
            body="Hello",
            in_reply_to="<a@x.com>",
            references="<a@x.com> <b@x.com>\r\n <c@x.com>",
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"References: <a@x.com> <b@x.com> <c@x.com>", raw)
        self.assertNotIn(b"References: <a@x.com> <b@x.com>\r\n", raw)

    def test_build_plain_mime_message_normalizes_bare_in_reply_to(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Thread test",
            body="Hello",
            in_reply_to="18137428606209368569",
            references="<18137428606209368569>",
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(b"In-Reply-To: <18137428606209368569>", raw)

    def test_explicit_identifiers_on_plain_mime_message(self) -> None:
        message_id = "<fixed-id@example.com>"
        date = "Mon, 01 Jan 2024 00:00:00 +0000"
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
            message_id=message_id,
            date=date,
            include_bcc_header=False,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        self.assertIn(f"Message-ID: {message_id}".encode(), raw)
        self.assertIn(f"Date: {date}".encode(), raw)


class Rfc5322LineLengthTests(unittest.TestCase):
    def test_reply_with_long_html_quote(self) -> None:
        long_html = '<p style="color:#000">' + ("x" * 2000) + "</p>"
        original = {
            "from": "Sender <s@example.com>",
            "date_received": "today",
            "body_html": long_html,
        }
        quoted_plain = quote_plain_reply(original, "short plain")
        body_html = build_outbound_html_for_compose(
            body_plain=f"My reply{quoted_plain}",
            mode="reply",
            reply_to=original,
            quoted_html_source=long_html,
            quoted_plain_expected=quoted_plain,
        )
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["contact@example.com"],
            cc=None,
            bcc=None,
            subject="Re: test",
            body=f"My reply{quoted_plain}",
            body_html=body_html,
            in_reply_to="<parent@example.com>",
            references="<parent@example.com>",
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        _assert_rfc5322_line_lengths(raw)

    def test_reply_with_long_url_in_plain_quote(self) -> None:
        long_url = "https://example.com/" + ("a" * 1500)
        original = {"from": "s@x.com", "date_received": "today"}
        quoted = quote_plain_reply(original, long_url)
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["contact@example.com"],
            cc=None,
            bcc=None,
            subject="Re: test",
            body=f"Thanks{quoted}",
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        _assert_rfc5322_line_lengths(raw)

    def test_new_message_with_long_plain_body(self) -> None:
        message = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="x" * 2000,
        )
        raw = _mime_message_raw_bytes(message)
        assert raw is not None
        _assert_rfc5322_line_lengths(raw)


if __name__ == "__main__":
    unittest.main()
