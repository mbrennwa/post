# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import searchable_body_text
from post.mail.search import (
    SearchTerm,
    filter_messages_by_query,
    parse_search_query,
    query_to_sexp,
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


class QueryToSexpTests(unittest.TestCase):
    def test_bare_word_searches_headers_and_body(self) -> None:
        query = parse_search_query("Core")
        assert query is not None
        sexp = query_to_sexp(query)
        self.assertIn('(header-contains "Subject" "Core")', sexp)
        self.assertIn('(body-contains "Core")', sexp)
        self.assertIn("(or ", sexp)

    def test_subject_prefix_is_header_only(self) -> None:
        query = parse_search_query("subject:Invoice")
        assert query is not None
        self.assertEqual(
            query_to_sexp(query),
            '(match-all (header-contains "Subject" "Invoice"))',
        )


class FilterMessagesByQueryTests(unittest.TestCase):
    def test_text_matches_headers(self) -> None:
        query = parse_search_query("klotz")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "hello", "from": "a@b.c", "flags": {"seen": True}},
            {"uid": "2", "subject": "Klotz am Band", "from": "a@b.c", "flags": {"seen": True}},
        ]
        matched = filter_messages_by_query(messages, query)
        self.assertEqual([message["uid"] for message in matched], ["2"])

    def test_text_term_uses_body_loader(self) -> None:
        query = parse_search_query("invoice")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "hello", "flags": {"seen": True}},
            {"uid": "2", "subject": "hello", "flags": {"seen": True}},
        ]

        def body_text(uid: str) -> str | None:
            return {"1": "nothing here", "2": "monthly invoice attached"}[uid]

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual([message["uid"] for message in matched], ["2"])

    def test_text_term_ignores_base64_data_uri_substrings(self) -> None:
        query = parse_search_query("ewz")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "Einladung", "flags": {"seen": True}},
        ]
        html = (
            "<p>eBaugesuche project invitation</p>"
            '<img src="data:image/png;base64,CHhEwzIZ0NcVewZoet">'
        )

        def body_text(_uid: str) -> str | None:
            return searchable_body_text(html=html)

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual(matched, [])

    def test_text_term_matches_readable_html_body(self) -> None:
        query = parse_search_query("ewz")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "Newsletter", "flags": {"seen": True}},
        ]
        html = (
            "<p>Contact the EWZ team about your project</p>"
            '<img src="data:image/png;base64,QUJDRA==">'
        )

        def body_text(_uid: str) -> str | None:
            return searchable_body_text(html=html)

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual([message["uid"] for message in matched], ["1"])

    def test_boolean_read_flag(self) -> None:
        query = parse_search_query("is:!read")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "a", "flags": {"seen": True}},
            {"uid": "2", "subject": "b", "flags": {"seen": False}},
        ]
        matched = filter_messages_by_query(messages, query)
        self.assertEqual([message["uid"] for message in matched], ["2"])

    def test_body_term_uses_loader(self) -> None:
        query = parse_search_query("body:invoice")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "hello", "flags": {"seen": True}},
            {"uid": "2", "subject": "hello", "flags": {"seen": True}},
        ]

        def body_text(uid: str) -> str | None:
            return {"1": "nothing here", "2": "monthly invoice attached"}[uid]

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual([message["uid"] for message in matched], ["2"])

    def test_cancelled_scan_stops_early(self) -> None:
        query = parse_search_query("body:needle")
        assert query is not None
        messages = [{"uid": str(index), "subject": "x", "flags": {}} for index in range(100)]
        calls = {"count": 0}

        def body_text(_uid: str) -> str | None:
            calls["count"] += 1
            return "haystack"

        cancelled = {"value": False}

        def is_cancelled() -> bool:
            if calls["count"] >= 5:
                cancelled["value"] = True
            return cancelled["value"]

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
            is_cancelled=is_cancelled,
        )
        self.assertEqual(matched, [])
        self.assertGreaterEqual(calls["count"], 5)
        self.assertLess(calls["count"], 100)

    def test_reports_filter_progress(self) -> None:
        from post.mail.search import SearchFilterProgress, format_search_filter_progress

        query = parse_search_query("invoice")
        assert query is not None
        messages = [
            {"uid": str(index), "subject": "monthly invoice", "flags": {}}
            for index in range(250)
        ]
        seen: list[SearchFilterProgress] = []

        matched = filter_messages_by_query(
            messages,
            query,
            on_progress=seen.append,
            progress_interval=100,
        )

        self.assertEqual(len(matched), 250)
        self.assertEqual(seen[0], SearchFilterProgress(0, 250, 0))
        self.assertEqual(seen[1], SearchFilterProgress(100, 250, 100))
        self.assertEqual(seen[2], SearchFilterProgress(200, 250, 200))
        self.assertEqual(seen[-1], SearchFilterProgress(250, 250, 250))
        self.assertEqual(
            format_search_filter_progress(seen[-1]),
            "Searching… 250 / 250 · 250 matches",
        )


if __name__ == "__main__":
    unittest.main()
