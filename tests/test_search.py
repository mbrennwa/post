# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.search import (
    SearchTerm,
    parse_search_query,
)


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
        query = parse_search_query("is:read is:flagged has:attachment")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="read"),
                SearchTerm(field="flagged"),
                SearchTerm(field="attachment"),
            ),
        )

    def test_boolean_negation(self) -> None:
        query = parse_search_query("is:!read is:!flagged has:!attachment")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="read", negated=True),
                SearchTerm(field="flagged", negated=True),
                SearchTerm(field="attachment", negated=True),
            ),
        )

    def test_boolean_spacing_after_colon(self) -> None:
        query = parse_search_query("is: read is: !flagged has: attachment")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="read"),
                SearchTerm(field="flagged", negated=True),
                SearchTerm(field="attachment"),
            ),
        )

    def test_boolean_case_insensitive(self) -> None:
        query = parse_search_query("IS:!READ Has:!Attachment")
        assert query is not None
        self.assertEqual(
            query.terms,
            (
                SearchTerm(field="read", negated=True),
                SearchTerm(field="attachment", negated=True),
            ),
        )

    def test_is_unread_is_not_boolean(self) -> None:
        query = parse_search_query("is:unread")
        assert query is not None
        self.assertEqual(query.terms, (SearchTerm(field="text", value="is:unread"),))

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


if __name__ == "__main__":
    unittest.main()
