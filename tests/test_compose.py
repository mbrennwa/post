# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.compose import (
    build_forward_subject,
    build_reply_all_recipients,
    build_reply_references,
    build_reply_subject,
    extract_reply_address,
    format_address_list,
    normalize_email,
    parse_address_header,
    parse_address_list,
    quote_plain_forward,
    quote_plain_reply,
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


class BuildForwardSubjectTests(unittest.TestCase):
    def test_adds_fwd_prefix(self) -> None:
        self.assertEqual(build_forward_subject("Hello"), "Fwd: Hello")

    def test_keeps_existing_fwd(self) -> None:
        self.assertEqual(build_forward_subject("Fwd: Hello"), "Fwd: Hello")

    def test_keeps_existing_fw(self) -> None:
        self.assertEqual(build_forward_subject("FW: Hello"), "FW: Hello")


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

    def test_no_recipients_raises(self) -> None:
        original = {
            "from": "Me <me@example.com>",
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


if __name__ == "__main__":
    unittest.main()
