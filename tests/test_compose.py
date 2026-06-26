# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.compose import (
    ComposeAttachment,
    body_html_for_quoting,
    body_text_for_quoting,
    build_draft_mime_message,
    build_outbound_email_bytes,
    build_outbound_mime_package,
    build_outbound_html_for_compose,
    build_plain_mime_message,
    build_forward_subject,
    build_reply_all_recipients,
    build_reply_references,
    build_reply_subject,
    extract_reply_address,
    extract_reply_target_addresses,
    format_address_list,
    normalize_email,
    parse_address_header,
    parse_address_list,
    quote_html_forward,
    quote_plain_forward,
    quote_plain_reply,
    read_compose_attachments_from_message,
    split_compose_body_at_quote,
)
from post.mail.helpers import (
    _mime_message_raw_bytes,
    extract_attachments,
    extract_message_bodies,
    get_attachment_data,
    html_to_quotable_plain,
    plain_body_looks_truncated,
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
            parse_address_list("mbrennwa@gmail.com <mbrennwa@gmail.com>"),
            ["mbrennwa@gmail.com"],
        )


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

    def _assert_rejects_outbound(self, **overrides: object) -> None:
        kwargs = {**self._BASE, "from_name": "Alice", "subject": "Hi", **overrides}
        with self.assertRaises(ValueError):
            build_outbound_email_bytes(**kwargs)

    def test_rejects_subject_injection_plain(self) -> None:
        self._assert_rejects_plain(subject=f"Hi{self._INJECT}")

    def test_rejects_subject_injection_draft(self) -> None:
        self._assert_rejects_draft(subject=f"Hi{self._INJECT}")

    def test_rejects_subject_injection_outbound(self) -> None:
        self._assert_rejects_outbound(subject=f"Hi{self._INJECT}")

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
            "from": "Matthias Brennwald <info@gasometrix.com>",
            "to": "matthias@brennwald, mbrennwa@gmail.com",
            "cc": "Carol <carol@example.com>",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={
                normalize_email("info@gasometrix.com"),
                normalize_email("matthias@brennwald"),
            },
        )
        self.assertEqual(
            to_addrs,
            ["mbrennwa@gmail.com"],
        )
        self.assertEqual(cc_addrs, ["Carol <carol@example.com>"])

    def test_reply_all_omits_cc_matching_compose_from(self) -> None:
        original = {
            "from": "Matthias Brennwald <info@gasometrix.com>",
            "to": "matthias@brennwald, mbrennwa@gmail.com, Matthias Brennwald <brennmat@gmail.com>",
            "cc": "info@gasometrix.com",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={
                normalize_email("info@gasometrix.com"),
                normalize_email("matthias@brennwald"),
            },
        )
        self.assertEqual(
            to_addrs,
            ["mbrennwa@gmail.com", "Matthias Brennwald <brennmat@gmail.com>"],
        )
        self.assertEqual(cc_addrs, [])

    def test_reply_all_omits_cc_addresses_already_in_to(self) -> None:
        original = {
            "from": "Matthias Brennwald <info@gasometrix.com>",
            "to": "mbrennwa@gmail.com",
            "cc": "info@gasometrix.com",
        }
        to_addrs, cc_addrs = build_reply_all_recipients(
            original,
            own_addresses={normalize_email("mbrennwa@gmail.com")},
        )
        self.assertEqual(
            to_addrs,
            ["Matthias Brennwald <info@gasometrix.com>"],
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


class FormatAddressListTests(unittest.TestCase):
    def test_joins_addresses(self) -> None:
        self.assertEqual(
            format_address_list(["a@example.com", "Bob <b@example.com>"]),
            "a@example.com, Bob <b@example.com>",
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


class SignatureComposeTests(unittest.TestCase):
    def test_format_signature_block(self) -> None:
        from post.mail.compose import compose_body_with_signature, format_signature_block

        self.assertEqual(format_signature_block(""), "")
        self.assertEqual(
            format_signature_block("Alice\nExample Corp"),
            "-- \nAlice\nExample Corp",
        )

    def test_new_message_body(self) -> None:
        from post.mail.compose import compose_body_with_signature

        self.assertEqual(
            compose_body_with_signature(
                mode="new",
                quoted_body="",
                signature="Alice",
            ),
            "\n\n-- \nAlice",
        )
        self.assertEqual(
            compose_body_with_signature(mode="new", quoted_body="", signature=""),
            "",
        )

    def test_reply_inserts_signature_before_quote(self) -> None:
        from post.mail.compose import compose_body_with_signature

        quoted = "\n\nOn today, a@b.com wrote:\n> hi\n"
        body = compose_body_with_signature(
            mode="reply",
            quoted_body=quoted,
            signature="Alice",
        )
        self.assertTrue(body.startswith("\n\n-- \nAlice\n\n"))
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

    def test_build_outbound_email_bytes_with_html_is_multipart_alternative(self) -> None:
        payload = build_outbound_email_bytes(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Fwd: Hi",
            body="See below\n",
            body_html='<blockquote class="post_quote"><p>Hi</p></blockquote>',
        )
        import email
        import email.policy

        parsed = email.message_from_bytes(payload, policy=email.policy.default)
        self.assertTrue(parsed.is_multipart())
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        plain_part = parsed.get_body(preferencelist=("plain",))
        html_part = parsed.get_body(preferencelist=("html",))
        self.assertIsNotNone(plain_part)
        self.assertIsNotNone(html_part)
        self.assertIn("See below", str(plain_part.get_content()))
        self.assertIn("post_quote", str(html_part.get_content()))

    def test_build_outbound_email_bytes_omits_bcc_from_wire(self) -> None:
        payload = build_outbound_email_bytes(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=["carol@example.com"],
            bcc=["secret@example.com"],
            subject="Hi",
            body="Hello",
        )
        import email
        import email.policy

        parsed = email.message_from_bytes(payload, policy=email.policy.default)
        self.assertEqual(parsed["To"], "bob@example.com")
        self.assertEqual(parsed["Cc"], "carol@example.com")
        self.assertNotIn("Bcc", parsed)
        self.assertNotIn(b"Bcc:", payload)

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


class OutboundMimeParityTests(unittest.TestCase):
    def test_build_outbound_email_bytes_includes_message_id_and_date(self) -> None:
        import email
        import email.policy

        payload = build_outbound_email_bytes(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
        )
        parsed = email.message_from_bytes(payload, policy=email.policy.SMTP)
        self.assertIsNotNone(parsed["Message-ID"])
        self.assertIsNotNone(parsed["Date"])
        self.assertTrue(str(parsed["Message-ID"]).startswith("<"))
        self.assertIn(b"\r\n", payload)

    def test_build_outbound_mime_package_wire_and_sent_share_identifiers(self) -> None:
        import email
        import email.policy

        package = build_outbound_mime_package(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=["carol@example.com"],
            bcc=["secret@example.com"],
            subject="Thread test",
            body="Hello",
            in_reply_to="<parent@example.com>",
            references="<parent@example.com>",
        )
        wire = email.message_from_bytes(
            package.wire_bytes, policy=email.policy.SMTP
        )
        sent_raw = _mime_message_raw_bytes(package.sent_message)
        assert sent_raw is not None

        self.assertEqual(wire["Message-ID"], package.message_id)
        self.assertEqual(wire["Date"], package.date)
        self.assertIn(f"Message-ID: {package.message_id}".encode(), sent_raw)
        self.assertIn(f"Date: {package.date}".encode(), sent_raw)
        self.assertEqual(wire["In-Reply-To"], "<parent@example.com>")
        self.assertEqual(wire["References"], "<parent@example.com>")
        self.assertNotIn("Bcc", wire)
        self.assertIn(b"Bcc:", sent_raw)

    def test_explicit_identifiers_are_shared_between_builders(self) -> None:
        import email
        import email.policy

        message_id = "<fixed-id@example.com>"
        date = "Mon, 01 Jan 2024 00:00:00 +0000"
        wire_bytes = build_outbound_email_bytes(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
            message_id=message_id,
            date=date,
        )
        sent = build_plain_mime_message(
            from_name="Alice",
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Hello",
            message_id=message_id,
            date=date,
        )
        wire = email.message_from_bytes(wire_bytes, policy=email.policy.SMTP)
        sent_raw = _mime_message_raw_bytes(sent)
        assert sent_raw is not None

        self.assertEqual(wire["Message-ID"], message_id)
        self.assertEqual(wire["Date"], date)
        self.assertIn(f"Message-ID: {message_id}".encode(), sent_raw)
        self.assertIn(f"Date: {date}".encode(), sent_raw)


if __name__ == "__main__":
    unittest.main()
