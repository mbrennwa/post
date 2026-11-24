# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.compose import normalize_email
from post.mail.correspondents import (
    Correspondent,
    aggregate_correspondents,
    apply_address_completion,
    collect_correspondents,
    correspondent_matches_prefix,
    current_address_token,
    folder_feeds_correspondents,
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

    def test_collects_bcc(self) -> None:
        correspondents = collect_correspondents(
            [
                {
                    "from": "Me <me@example.com>",
                    "to": "Alice <alice@example.com>",
                    "bcc": "Secret <secret@example.com>",
                    "sort_date": 10,
                }
            ]
        )
        emails = {item.email for item in correspondents}
        self.assertIn(normalize_email("secret@example.com"), emails)

    def test_prefers_newer_display(self) -> None:
        correspondents = collect_correspondents(
            [
                {
                    "from": "Alice <alice@example.com>",
                    "to": "",
                    "cc": "",
                    "sort_date": 1,
                },
                {
                    "from": "Alice Smith <alice@example.com>",
                    "to": "",
                    "cc": "",
                    "sort_date": 9,
                },
            ]
        )
        self.assertEqual(len(correspondents), 1)
        self.assertEqual(correspondents[0].display, "Alice Smith <alice@example.com>")
        self.assertEqual(correspondents[0].last_seen, 9)


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
            [{"from": "공병채 <researcher@lab.example.com>", "to": "", "cc": ""}]
        )
        matches = match_correspondents(correspondents, "lab")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("researcher@lab.example.com"))

    def test_match_by_at_domain_prefix(self) -> None:
        correspondents = collect_correspondents(
            [{"from": "공병채 <researcher@lab.example.com>", "to": "", "cc": ""}]
        )
        matches = match_correspondents(correspondents, "@lab")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].email, normalize_email("researcher@lab.example.com"))

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
            [{"from": "공병채 <researcher@lab.example.com>", "to": "", "cc": ""}]
        )[0]
        self.assertTrue(correspondent_matches_prefix(correspondent, "lab"))


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


class FolderFeedsCorrespondentsTests(unittest.TestCase):
    def test_includes_inbox_sent_archive_trash(self) -> None:
        for name in ("INBOX", "Sent", "Sent Items", "Archive", "Trash"):
            self.assertTrue(folder_feeds_correspondents(name), name)

    def test_skips_junk_drafts_outbox(self) -> None:
        for name in ("Junk", "Spam", "Drafts", "Draft", ".post/Outbox"):
            self.assertFalse(folder_feeds_correspondents(name), name)


class AggregateCorrespondentsTests(unittest.TestCase):
    def test_keeps_newer_display(self) -> None:
        older = collect_correspondents(
            [{"from": "Alice <alice@example.com>", "sort_date": 1}]
        )
        newer = collect_correspondents(
            [{"from": "Alice S. <alice@example.com>", "sort_date": 5}]
        )
        merged = aggregate_correspondents([older, newer])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].display, "Alice S. <alice@example.com>")
        self.assertEqual(merged[0].last_seen, 5)

    def test_adds_new_email(self) -> None:
        first = collect_correspondents(
            [{"from": "Alice <alice@example.com>", "sort_date": 1}]
        )
        second = collect_correspondents(
            [{"from": "Bob <bob@example.com>", "sort_date": 2}]
        )
        merged = aggregate_correspondents([first, second])
        emails = {item.email for item in merged}
        self.assertEqual(
            emails,
            {
                normalize_email("alice@example.com"),
                normalize_email("bob@example.com"),
            },
        )

    def test_sums_counts_and_takes_latest_timestamps(self) -> None:
        inbox = collect_correspondents(
            [
                {"from": "Alice <alice@example.com>", "sort_date": 10},
                {"from": "Alice <alice@example.com>", "sort_date": 30},
            ]
        )
        sent = collect_correspondents(
            [{"from": "Me <me@example.com>", "to": "alice@example.com", "sort_date": 20}],
            is_sent_folder=True,
        )
        merged = {item.email: item for item in aggregate_correspondents([inbox, sent])}
        alice = merged["alice@example.com"]
        self.assertEqual(alice.received_count, 2)
        self.assertEqual(alice.sent_count, 1)
        self.assertEqual(alice.last_received, 30)
        self.assertEqual(alice.last_sent, 20)
        self.assertEqual(alice.last_seen, 30)


class CollectCountsTests(unittest.TestCase):
    def test_received_counts_from_header_only(self) -> None:
        items = {
            item.email: item
            for item in collect_correspondents(
                [
                    {
                        "from": "Alice <alice@example.com>",
                        "to": "Me <me@example.com>",
                        "cc": "Carol <carol@example.com>",
                        "sort_date": 7,
                    }
                ]
            )
        }
        self.assertEqual(items["alice@example.com"].received_count, 1)
        self.assertEqual(items["alice@example.com"].last_received, 7)
        for email in ("me@example.com", "carol@example.com"):
            self.assertEqual(items[email].received_count, 0)
            self.assertEqual(items[email].sent_count, 0)
            self.assertEqual(items[email].last_seen, 7)

    def test_sent_folder_counts_recipients_and_keeps_own_address(self) -> None:
        items = {
            item.email: item
            for item in collect_correspondents(
                [
                    {
                        "from": "Me <me@example.com>",
                        "to": "Alice <alice@example.com>",
                        "cc": "Alice <alice@example.com>",
                        "bcc": "Me <me@example.com>",
                        "sort_date": 5,
                    }
                ],
                is_sent_folder=True,
            )
        }
        self.assertEqual(items["alice@example.com"].sent_count, 1)
        self.assertEqual(items["alice@example.com"].last_sent, 5)
        # Own address stays suggestible; counted once via Bcc, not via From.
        self.assertEqual(items["me@example.com"].sent_count, 1)
        self.assertEqual(items["me@example.com"].received_count, 0)

    def test_sent_from_header_is_presence_only(self) -> None:
        items = collect_correspondents(
            [{"from": "Me <me@example.com>", "to": "", "sort_date": 3}],
            is_sent_folder=True,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].sent_count, 0)
        self.assertEqual(items[0].received_count, 0)
        self.assertEqual(items[0].last_seen, 3)

    def test_collecting_same_messages_twice_is_idempotent(self) -> None:
        messages = [
            {"from": "Alice <alice@example.com>", "sort_date": 1},
            {"from": "Alice <alice@example.com>", "sort_date": 2},
        ]
        self.assertEqual(
            collect_correspondents(messages), collect_correspondents(messages)
        )
        self.assertEqual(collect_correspondents(messages)[0].received_count, 2)


NOW = 1_000_000_000
DAY = 24 * 3600


def _person(
    email: str,
    *,
    name: str = "",
    sent: int = 0,
    received: int = 0,
    last_sent: int = 0,
    last_received: int = 0,
    last_seen: int | None = None,
) -> Correspondent:
    return Correspondent(
        display=f"{name} <{email}>" if name else email,
        email=email,
        name=name,
        last_seen=max(last_sent, last_received) if last_seen is None else last_seen,
        sent_count=sent,
        received_count=received,
        last_sent=last_sent,
        last_received=last_received,
    )


class RankingTests(unittest.TestCase):
    def _emails(self, candidates, prefix, **kwargs):
        return [
            item.email
            for item in match_correspondents(candidates, prefix, now=NOW, **kwargs)
        ]

    def test_frequent_sent_address_beats_fresh_newsletter(self) -> None:
        candidates = [
            _person("news@shop.example", received=1, last_received=NOW - 60),
            _person(
                "nina@work.example",
                name="Nina",
                sent=12,
                last_sent=NOW - 10 * DAY,
            ),
        ]
        self.assertEqual(
            self._emails(candidates, "n"),
            ["nina@work.example", "news@shop.example"],
        )

    def test_any_sent_address_outranks_any_received_only_address(self) -> None:
        candidates = [
            _person("chatty@x.example", received=5000, last_received=NOW),
            _person("chosen@x.example", sent=1, last_sent=NOW),
        ]
        self.assertEqual(
            self._emails(candidates, "ch"),
            ["chosen@x.example", "chatty@x.example"],
        )

    def test_frequency_beats_name_before_email_tier(self) -> None:
        candidates = [
            _person("m.rare@a.example", name="Mark", sent=1, last_sent=NOW - 300 * DAY),
            _person("marketing@b.example", sent=40, last_sent=NOW - 2 * DAY),
        ]
        self.assertEqual(
            self._emails(candidates, "mar"),
            ["marketing@b.example", "m.rare@a.example"],
        )

    def test_match_tier_breaks_ties(self) -> None:
        candidates = [
            _person("zed@example.com", sent=2, last_sent=NOW - DAY, last_seen=5),
            _person(
                "other@example.com",
                name="Zed Other",
                sent=2,
                last_sent=NOW - DAY,
                last_seen=5,
            ),
        ]
        self.assertEqual(
            self._emails(candidates, "zed"),
            ["other@example.com", "zed@example.com"],
        )

    def test_recency_decays_old_frequency(self) -> None:
        candidates = [
            _person("old@example.com", sent=5, last_sent=NOW - 5 * 365 * DAY),
            _person("new@example.com", sent=5, last_sent=NOW - DAY),
        ]
        self.assertEqual(
            self._emails(candidates, "@example"),
            ["new@example.com", "old@example.com"],
        )

    def test_future_dates_do_not_boost_score(self) -> None:
        candidates = [
            _person("spam@example.com", received=1, last_received=NOW + 10 * 365 * DAY),
            _person("real@example.com", sent=3, last_sent=NOW - 30 * DAY),
        ]
        self.assertEqual(
            self._emails(candidates, "@example"),
            ["real@example.com", "spam@example.com"],
        )

    def test_equal_scores_fall_back_to_recency_then_email(self) -> None:
        candidates = [
            _person("b@example.com", last_seen=10),
            _person("a@example.com", last_seen=10),
            _person("c@example.com", last_seen=20),
        ]
        self.assertEqual(
            self._emails(candidates, "@example"),
            ["c@example.com", "a@example.com", "b@example.com"],
        )

    def test_limit_applies_after_ranking(self) -> None:
        candidates = [
            _person("a1@example.com", received=1, last_received=NOW),
            _person("a2@example.com", sent=9, last_sent=NOW),
        ]
        self.assertEqual(self._emails(candidates, "a", limit=1), ["a2@example.com"])


if __name__ == "__main__":
    unittest.main()
