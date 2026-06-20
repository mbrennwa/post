# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.compose import (
    build_reply_references,
    build_reply_subject,
    extract_reply_address,
    parse_address_list,
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


if __name__ == "__main__":
    unittest.main()
