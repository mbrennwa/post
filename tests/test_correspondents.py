# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.compose import normalize_email
from post.mail.correspondents import (
    apply_address_completion,
    collect_correspondents,
    correspondent_matches_prefix,
    current_address_token,
    match_correspondents,
)


class CollectCorrespondentsTests(unittest.TestCase):
    def test_collects_from_to_and_cc(self) -> None:
        messages = [
            {
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "cc": "Carol <carol@example.com>",
            }
        ]
        correspondents = collect_correspondents(messages)
        emails = {item.email for item in correspondents}
        self.assertEqual(
            emails,
            {
                normalize_email("alice@example.com"),
                normalize_email("bob@example.com"),
                normalize_email("carol@example.com"),
            },
        )

    def test_deduplicates_by_email(self) -> None:
        messages = [
            {
                "from": "Alice <alice@example.com>",
                "to": "Alice Smith <alice@example.com>",
                "cc": "",
            },
            {
                "from": "Bob <bob@example.com>",
                "to": "Alice <alice@example.com>",
                "cc": "",
            },
        ]
        correspondents = collect_correspondents(messages)
        self.assertEqual(len(correspondents), 2)
        self.assertEqual(correspondents[0].display, "Alice <alice@example.com>")
        self.assertEqual(correspondents[1].display, "Bob <bob@example.com>")

    def test_excludes_own_addresses(self) -> None:
        messages = [
            {
                "from": "Me <me@example.com>",
                "to": "Bob <bob@example.com>",
                "cc": "",
            }
        ]
        correspondents = collect_correspondents(
            messages,
            exclude_emails={normalize_email("me@example.com")},
        )
        self.assertEqual(len(correspondents), 1)
        self.assertEqual(correspondents[0].email, normalize_email("bob@example.com"))


class CurrentAddressTokenTests(unittest.TestCase):
    def test_single_token(self) -> None:
        self.assertEqual(current_address_token("bob"), "bob")

    def test_last_token_after_comma(self) -> None:
        self.assertEqual(
            current_address_token("alice@example.com, bob"),
            "bob",
        )

    def test_quoted_name_with_comma(self) -> None:
        self.assertEqual(
            current_address_token('"Last, First" <person@example.com>, next'),
            "next",
        )


class MatchCorrespondentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = collect_correspondents(
            [
                {
                    "from": "Alice <alice@example.com>",
                    "to": "Bob <bob@example.com>",
                    "cc": "Carol <carol@example.com>",
                }
            ]
        )

    def test_match_by_name_prefix(self) -> None:
        matches = match_correspondents(self.candidates, "ali")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, "Alice")

    def test_match_by_email_prefix(self) -> None:
        matches = match_correspondents(self.candidates, "bob@")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("bob@example.com"))

    def test_match_by_email_domain_prefix(self) -> None:
        matches = match_correspondents(self.candidates, "bob@ex")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("bob@example.com"))

    def test_match_by_bare_domain_prefix(self) -> None:
        correspondents = collect_correspondents(
            [{"from": "공병채 <kong3512@kigam.re.kr>", "to": "", "cc": ""}]
        )
        matches = match_correspondents(correspondents, "kigam")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("kong3512@kigam.re.kr"))

    def test_match_by_at_domain_prefix(self) -> None:
        correspondents = collect_correspondents(
            [{"from": "공병채 <kong3512@kigam.re.kr>", "to": "", "cc": ""}]
        )
        matches = match_correspondents(correspondents, "@kigam")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("kong3512@kigam.re.kr"))

    def test_empty_prefix_returns_empty(self) -> None:
        self.assertEqual(match_correspondents(self.candidates, ""), [])

    def test_respects_limit(self) -> None:
        matches = match_correspondents(self.candidates, "a", limit=1)
        self.assertEqual(len(matches), 1)


class CorrespondentMatchesPrefixTests(unittest.TestCase):
    def test_matches_name(self) -> None:
        correspondent = collect_correspondents(
            [{"from": "Alice <alice@example.com>", "to": "", "cc": ""}]
        )[0]
        self.assertTrue(correspondent_matches_prefix(correspondent, "ali"))

    def test_matches_email(self) -> None:
        correspondent = collect_correspondents(
            [{"from": "Bob <bob@example.com>", "to": "", "cc": ""}]
        )[0]
        self.assertTrue(correspondent_matches_prefix(correspondent, "bob@ex"))

    def test_matches_domain(self) -> None:
        correspondent = collect_correspondents(
            [{"from": "공병채 <kong3512@kigam.re.kr>", "to": "", "cc": ""}]
        )[0]
        self.assertTrue(correspondent_matches_prefix(correspondent, "kigam"))


class ApplyAddressCompletionTests(unittest.TestCase):
    def test_replaces_single_token(self) -> None:
        self.assertEqual(
            apply_address_completion("bo", "Bob <bob@example.com>"),
            "Bob <bob@example.com>, ",
        )

    def test_preserves_previous_recipients(self) -> None:
        self.assertEqual(
            apply_address_completion(
                "alice@example.com, bo",
                "Bob <bob@example.com>",
            ),
            "alice@example.com, Bob <bob@example.com>, ",
        )


if __name__ == "__main__":
    unittest.main()
