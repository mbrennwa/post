# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.mailto import MailtoCompose, parse_mailto_uri


class ParseMailtoUriTests(unittest.TestCase):
    def test_rejects_non_mailto(self) -> None:
        with self.assertRaises(ValueError):
            parse_mailto_uri("https://example.com")

    def test_simple_address(self) -> None:
        result = parse_mailto_uri("mailto:alice@example.com")
        self.assertEqual(result, MailtoCompose(to=("alice@example.com",)))

    def test_multiple_path_addresses(self) -> None:
        result = parse_mailto_uri("mailto:alice@example.com,bob@example.com")
        self.assertEqual(
            result.to, ("alice@example.com", "bob@example.com")
        )

    def test_query_headers(self) -> None:
        result = parse_mailto_uri(
            "mailto:alice@example.com"
            "?subject=Hello%20there"
            "&cc=carol@example.com"
            "&bcc=dave@example.com"
            "&body=Line%201%0ALine%202"
        )
        self.assertEqual(result.to, ("alice@example.com",))
        self.assertEqual(result.cc, ("carol@example.com",))
        self.assertEqual(result.bcc, ("dave@example.com",))
        self.assertEqual(result.subject, "Hello there")
        self.assertEqual(result.body, "Line 1\nLine 2")

    def test_to_in_query_merges_with_path(self) -> None:
        result = parse_mailto_uri(
            "mailto:alice@example.com?to=bob@example.com,carol@example.com"
        )
        self.assertEqual(
            result.to,
            ("alice@example.com", "bob@example.com", "carol@example.com"),
        )

    def test_empty_path_with_to_query(self) -> None:
        result = parse_mailto_uri("mailto:?to=alice@example.com&subject=Hi")
        self.assertEqual(result.to, ("alice@example.com",))
        self.assertEqual(result.subject, "Hi")

    def test_dedupes_addresses_case_insensitively(self) -> None:
        result = parse_mailto_uri(
            "mailto:Alice@Example.com?to=alice@example.com&cc=Alice@example.com"
        )
        self.assertEqual(result.to, ("Alice@Example.com",))
        self.assertEqual(result.cc, ("Alice@example.com",))

    def test_percent_encoded_display_name(self) -> None:
        result = parse_mailto_uri(
            "mailto:Alice%20%3Calice@example.com%3E"
        )
        self.assertEqual(result.to, ("Alice <alice@example.com>",))

    def test_crlf_body_normalized_to_lf(self) -> None:
        result = parse_mailto_uri("mailto:?body=a%0D%0Ab")
        self.assertEqual(result.body, "a\nb")


if __name__ == "__main__":
    unittest.main()
