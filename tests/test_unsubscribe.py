# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from post.mail.helpers import (
    has_one_click_unsubscribe_post,
    parse_list_unsubscribe_uris,
    perform_one_click_unsubscribe,
    unsubscribe_action_from_headers,
)


class ParseListUnsubscribeUrisTests(unittest.TestCase):
    def test_angle_bracket_https_and_mailto(self) -> None:
        uris = parse_list_unsubscribe_uris(
            "<https://example.com/unsub>, <mailto:off@example.com>"
        )
        self.assertEqual(
            uris,
            ["https://example.com/unsub", "mailto:off@example.com"],
        )

    def test_rejects_non_http_mailto_schemes(self) -> None:
        uris = parse_list_unsubscribe_uris(
            "<ftp://example.com/x>, <javascript:alert(1)>, <https://ok.example/>"
        )
        self.assertEqual(uris, ["https://ok.example/"])

    def test_comma_separated_without_brackets(self) -> None:
        uris = parse_list_unsubscribe_uris(
            "https://example.com/a, mailto:list-off@example.com"
        )
        self.assertEqual(
            uris,
            ["https://example.com/a", "mailto:list-off@example.com"],
        )

    def test_empty_and_none(self) -> None:
        self.assertEqual(parse_list_unsubscribe_uris(None), [])
        self.assertEqual(parse_list_unsubscribe_uris(""), [])
        self.assertEqual(parse_list_unsubscribe_uris("   "), [])


class OneClickPostHeaderTests(unittest.TestCase):
    def test_detects_rfc8058_token(self) -> None:
        self.assertTrue(
            has_one_click_unsubscribe_post("List-Unsubscribe=One-Click")
        )

    def test_ignores_unrelated(self) -> None:
        self.assertFalse(has_one_click_unsubscribe_post("Something-Else"))
        self.assertFalse(has_one_click_unsubscribe_post(None))


class UnsubscribeActionFromHeadersTests(unittest.TestCase):
    def test_prefers_https_post_when_one_click(self) -> None:
        action = unsubscribe_action_from_headers(
            "<mailto:off@example.com>, <https://example.com/unsub>",
            "List-Unsubscribe=One-Click",
        )
        self.assertEqual(
            action,
            {"kind": "post", "url": "https://example.com/unsub"},
        )

    def test_does_not_post_to_http(self) -> None:
        action = unsubscribe_action_from_headers(
            "<http://example.com/unsub>",
            "List-Unsubscribe=One-Click",
        )
        self.assertEqual(
            action,
            {"kind": "open", "url": "http://example.com/unsub"},
        )

    def test_prefers_https_open_without_post_header(self) -> None:
        action = unsubscribe_action_from_headers(
            "<mailto:off@example.com>, <https://example.com/unsub>",
            None,
        )
        self.assertEqual(
            action,
            {"kind": "open", "url": "https://example.com/unsub"},
        )

    def test_prefers_https_for_mailman_options_page(self) -> None:
        action = unsubscribe_action_from_headers(
            "<https://lists.gnu.org/mailman/options/help-octave>, "
            "<mailto:help-octave-leave@gnu.org>",
            None,
        )
        self.assertEqual(
            action,
            {
                "kind": "open",
                "url": "https://lists.gnu.org/mailman/options/help-octave",
            },
        )

    def test_falls_back_to_mailto(self) -> None:
        action = unsubscribe_action_from_headers(
            "<mailto:off@example.com>",
            None,
        )
        self.assertEqual(
            action,
            {"kind": "open", "url": "mailto:off@example.com"},
        )

    def test_none_without_usable_uris(self) -> None:
        self.assertIsNone(unsubscribe_action_from_headers(None, None))
        self.assertIsNone(
            unsubscribe_action_from_headers("<ftp://example.com/x>", None)
        )


class PerformOneClickUnsubscribeTests(unittest.TestCase):
    def test_rejects_non_https(self) -> None:
        with self.assertRaises(ValueError):
            perform_one_click_unsubscribe("http://example.com/unsub")

    def test_posts_one_click_body(self) -> None:
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch(
            "urllib.request.urlopen", return_value=response
        ) as urlopen:
            perform_one_click_unsubscribe("https://example.com/unsub")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/unsub")
        self.assertEqual(request.data, b"List-Unsubscribe=One-Click")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Content-type"),
            "application/x-www-form-urlencoded",
        )


if __name__ == "__main__":
    unittest.main()
