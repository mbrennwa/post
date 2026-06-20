# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.search import (
    MessageSearchQuery,
    SearchTerm,
    message_matches,
    parse_search_query,
)


def _msg(
    *,
    subject: str = "Hello",
    from_addr: str = "Alice <alice@example.com>",
    to_addr: str = "bob@example.com",
    cc: str = "",
    seen: bool = True,
    flagged: bool = False,
    attachments: bool = False,
) -> dict:
    return {
        "subject": subject,
        "from": from_addr,
        "to": to_addr,
        "cc": cc,
        "flags": {
            "seen": seen,
            "flagged": flagged,
            "attachments": attachments,
        },
    }


class ParseSearchQueryTests(unittest.TestCase):
    def test_empty_returns_none(self) -> None:
        self.assertIsNone(parse_search_query(""))
        self.assertIsNone(parse_search_query("   "))

    def test_bare_words_match_text_terms(self) -> None:
        query = parse_search_query("hello world")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="text", value="hello"),
                SearchTerm(field="text", value="world"),
            ),
        )

    def test_bare_word_single(self) -> None:
        query = parse_search_query("Auburn")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="text", value="Auburn"),))

    def test_unknown_prefix_becomes_text(self) -> None:
        query = parse_search_query("foo:bar")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="text", value="foo:bar"),))

    def test_from_prefix(self) -> None:
        query = parse_search_query("from:alice")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="from", value="alice"),))

    def test_subject_prefix(self) -> None:
        query = parse_search_query("subject:invoice")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="subject", value="invoice"),))

    def test_subject_prefix_with_space_after_colon(self) -> None:
        query = parse_search_query("subject: Auburn")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="subject", value="Auburn"),))

    def test_subject_multi_word_without_quotes(self) -> None:
        query = parse_search_query("subject: Auburn University")
        assert query is not None
        self.assertEqual(
            query.terms, (SearchTerm(field="subject", value="Auburn University"),)
        )

    def test_quoted_subject(self) -> None:
        query = parse_search_query('subject:"quarterly report"')
        assert query is not None
        self.assertEqual(
            query.terms, (SearchTerm(field="subject", value="quarterly report"),)
        )

    def test_combined_prefixes(self) -> None:
        query = parse_search_query("from:alice subject:invoice")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="from", value="alice"),
                SearchTerm(field="subject", value="invoice"),
            ),
        )

    def test_boolean_prefixes(self) -> None:
        query = parse_search_query("is:unread is:flagged has:attachment")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="unread", value=None),
                SearchTerm(field="flagged", value=None),
                SearchTerm(field="attachment", value=None),
            ),
        )

    def test_mixed_with_unknown_ignored(self) -> None:
        query = parse_search_query("from:alice unknown:foo subject:test")
        assert query is not None
        self.assertEqual(len(query.terms), 2)

    def test_combined_with_space_after_colon(self) -> None:
        query = parse_search_query("from: alice subject: Auburn")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="from", value="alice"),
                SearchTerm(field="subject", value="Auburn"),
            ),
        )

    def test_mixed_text_and_prefix(self) -> None:
        query = parse_search_query("Auburn from:alice")
        assert query is not None
        self.assertEqual(
            set(query.terms),
            {
                SearchTerm(field="text", value="Auburn"),
                SearchTerm(field="from", value="alice"),
            },
        )


class MessageMatchesTests(unittest.TestCase):
    def test_from_match(self) -> None:
        query = MessageSearchQuery(terms=(SearchTerm(field="from", value="alice"),))
        self.assertTrue(message_matches(_msg(from_addr="Alice <a@b.com>"), query))
        self.assertFalse(message_matches(_msg(from_addr="Bob <b@b.com>"), query))

    def test_subject_case_insensitive(self) -> None:
        query = MessageSearchQuery(
            terms=(SearchTerm(field="subject", value="INVOICE"),)
        )
        self.assertTrue(message_matches(_msg(subject="Your invoice"), query))

    def test_to_and_cc(self) -> None:
        query = MessageSearchQuery(terms=(SearchTerm(field="to", value="bob@"),))
        self.assertTrue(message_matches(_msg(to_addr="bob@example.com"), query))

        cc_query = MessageSearchQuery(terms=(SearchTerm(field="cc", value="team"),))
        self.assertTrue(message_matches(_msg(cc="team-list@example.com"), cc_query))

    def test_unread_flag(self) -> None:
        query = MessageSearchQuery(terms=(SearchTerm(field="unread", value=None),))
        self.assertTrue(message_matches(_msg(seen=False), query))
        self.assertFalse(message_matches(_msg(seen=True), query))

    def test_flagged_and_attachment(self) -> None:
        flagged = MessageSearchQuery(terms=(SearchTerm(field="flagged", value=None),))
        self.assertTrue(message_matches(_msg(flagged=True), flagged))

        attach = MessageSearchQuery(
            terms=(SearchTerm(field="attachment", value=None),)
        )
        self.assertTrue(message_matches(_msg(attachments=True), attach))

    def test_and_semantics(self) -> None:
        query = MessageSearchQuery(
            terms=(
                SearchTerm(field="from", value="alice"),
                SearchTerm(field="subject", value="invoice"),
            )
        )
        self.assertTrue(
            message_matches(
                _msg(from_addr="alice@x.com", subject="invoice #42"), query
            )
        )
        self.assertFalse(
            message_matches(_msg(from_addr="alice@x.com", subject="hello"), query)
        )

    def test_text_matches_any_default_field(self) -> None:
        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="Auburn"),))
        self.assertTrue(message_matches(_msg(subject="Fwd: miniREUDI for Auburn"), query))
        self.assertTrue(message_matches(_msg(from_addr="Auburn <a@b.com>"), query))
        self.assertFalse(message_matches(_msg(subject="H2 Deconvolution"), query))

    def test_text_and_prefix_combined(self) -> None:
        query = MessageSearchQuery(
            terms=(
                SearchTerm(field="text", value="Auburn"),
                SearchTerm(field="from", value="alice"),
            )
        )
        self.assertTrue(
            message_matches(
                _msg(from_addr="Alice <alice@x.com>", subject="Auburn news"), query
            )
        )
        self.assertFalse(
            message_matches(
                _msg(from_addr="Bob <bob@x.com>", subject="Auburn news"), query
            )
        )


if __name__ == "__main__":
    unittest.main()
