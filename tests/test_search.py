# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import searchable_body_text
from post.mail.search import (
    SearchTerm,
    filter_messages_by_query,
    parse_search_query,
    query_requires_body_scan,
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


class QueryRequiresBodyScanTests(unittest.TestCase):
    def test_header_and_boolean_terms_skip_body_scan(self) -> None:
        for raw in ("from:rebecca", "to:bob", "subject:hi", "cc:alice", "is:read"):
            with self.subTest(raw=raw):
                query = parse_search_query(raw)
                assert query is not None
                self.assertFalse(query_requires_body_scan(query))

    def test_text_and_body_terms_require_body_scan(self) -> None:
        for raw in ("rebecca", "body:invoice", "invoice from:alice"):
            with self.subTest(raw=raw):
                query = parse_search_query(raw)
                assert query is not None
                self.assertTrue(query_requires_body_scan(query))


class FilterMessagesByQueryTests(unittest.TestCase):
    def test_from_prefix_matches_sender(self) -> None:
        query = parse_search_query("from:rebecca")
        assert query is not None
        messages = [
            {
                "uid": "1",
                "subject": "hello",
                "from": "Rebecca Smith <rebecca@example.com>",
                "flags": {"seen": True},
            },
            {
                "uid": "2",
                "subject": "rebecca in subject only",
                "from": "Other <other@example.com>",
                "flags": {"seen": True},
            },
        ]
        matched = filter_messages_by_query(messages, query)
        self.assertEqual([message["uid"] for message in matched], ["1"])

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

    def test_text_term_ignores_tracking_url_in_plain_when_html_is_clean(self) -> None:
        query = parse_search_query("ewz")
        assert query is not None
        messages = [
            {
                "uid": "1",
                "subject": "diyAudio notification",
                "from": "diyAudio <contact@mail.diyaudio.com>",
                "flags": {"seen": True},
            },
        ]

        def body_text(_uid: str) -> str | None:
            return searchable_body_text(
                plain=(
                    "View this direct message:\n"
                    "https://www.diyaudio.com/direct-messages/abc?token=xEwzNoise"
                ),
                html=(
                    "<p>pras1170 has sent you a direct message at diyAudio.</p>"
                    "<p><a href=\"https://www.diyaudio.com/direct-messages/abc?"
                    "token=xEwzNoise\">View this direct message</a></p>"
                ),
            )

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual(matched, [])

    def test_text_term_prefers_visible_html_over_plain_urls(self) -> None:
        query = parse_search_query("ewz")
        assert query is not None
        messages = [
            {"uid": "1", "subject": "Newsletter", "flags": {"seen": True}},
        ]

        def body_text(_uid: str) -> str | None:
            return searchable_body_text(
                plain="https://news.example.com/track?payload=EwzToken",
                html="<p>Contact the EWZ team about your project</p>",
            )

        matched = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=body_text,
        )
        self.assertEqual([message["uid"] for message in matched], ["1"])

    def test_text_term_ignores_opaque_substrings_in_plain_urls(self) -> None:
        query = parse_search_query("ewz")
        assert query is not None
        messages = [{"uid": "1", "subject": "Hello", "flags": {"seen": True}}]

        def body_text(_uid: str) -> str | None:
            return searchable_body_text(
                plain="Open this link: https://example.com/track?payload=xEwzNoise",
            )

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
        self.assertEqual(seen[1], SearchFilterProgress(1, 250, 1))
        self.assertEqual(seen[2], SearchFilterProgress(100, 250, 100))
        self.assertEqual(seen[3], SearchFilterProgress(200, 250, 200))
        self.assertEqual(seen[-1], SearchFilterProgress(250, 250, 250))
        self.assertEqual(
            format_search_filter_progress(seen[-1]),
            "Searching…",
        )

    def test_reports_match_batches(self) -> None:
        query = parse_search_query("invoice")
        assert query is not None
        messages = [
            {"uid": str(index), "subject": "monthly invoice", "flags": {}}
            for index in range(52)
        ]
        batches: list[list[dict]] = []

        matched = filter_messages_by_query(
            messages,
            query,
            on_matches=batches.append,
            match_batch_size=25,
        )

        self.assertEqual(len(matched), 52)
        self.assertGreaterEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 1)
        streamed = [message for batch in batches for message in batch]
        self.assertEqual(streamed, matched)

    def test_cancellation_stops_match_batches(self) -> None:
        query = parse_search_query("invoice")
        assert query is not None
        messages = [
            {"uid": str(index), "subject": "monthly invoice", "flags": {}}
            for index in range(100)
        ]
        batches: list[list[dict]] = []
        cancelled = {"value": False}

        def on_matches(batch: list[dict]) -> None:
            batches.append(batch)
            if sum(len(item) for item in batches) >= 20:
                cancelled["value"] = True

        matched = filter_messages_by_query(
            messages,
            query,
            is_cancelled=lambda: cancelled["value"],
            on_matches=on_matches,
            match_batch_size=10,
        )

        self.assertLess(len(matched), 100)
        self.assertLess(sum(len(batch) for batch in batches), 100)


class SearchScopeHelpersTests(unittest.TestCase):
    def test_format_search_result_meta_joins_parts(self) -> None:
        from post.mail.search import format_search_result_meta

        self.assertEqual(
            format_search_result_meta("Work", "Inbox", "alice@example.com"),
            "Work · Inbox · alice@example.com",
        )

    def test_format_all_mail_progress_includes_folder_counts(self) -> None:
        from post.mail.search import SearchFilterProgress, format_search_filter_progress

        progress = SearchFilterProgress(
            40,
            120,
            3,
            account_label="Work",
            folder_label="Sent",
            folders_done=2,
            folders_total=5,
        )
        self.assertEqual(
            format_search_filter_progress(progress),
            "Searching Work/Sent…",
        )

    def test_format_search_target_label_joins_account_and_folder(self) -> None:
        from post.mail.search import format_search_target_label

        self.assertEqual(
            format_search_target_label(account_label="Work", folder_label="Inbox"),
            "Work/Inbox",
        )
        self.assertEqual(
            format_search_target_label(folder_label="Inbox"),
            "Inbox",
        )

    def test_search_filter_progress_fraction_single_folder(self) -> None:
        from post.mail.search import SearchFilterProgress, search_filter_progress_fraction

        self.assertEqual(
            search_filter_progress_fraction(SearchFilterProgress(50, 100, 2)),
            0.5,
        )

    def test_search_filter_progress_fraction_multi_folder(self) -> None:
        from post.mail.search import SearchFilterProgress, search_filter_progress_fraction

        progress = SearchFilterProgress(
            40,
            120,
            3,
            folder_label="Sent",
            folders_done=2,
            folders_total=5,
        )
        self.assertAlmostEqual(
            search_filter_progress_fraction(progress),
            (1 + 40 / 120) / 5,
        )

    def test_search_filter_progress_fraction_empty_folder(self) -> None:
        from post.mail.search import SearchFilterProgress, search_filter_progress_fraction

        progress = SearchFilterProgress(
            0,
            0,
            0,
            folders_done=3,
            folders_total=5,
        )
        self.assertAlmostEqual(search_filter_progress_fraction(progress), 0.6)


class FilterMessagesYieldTests(unittest.TestCase):
    def test_filter_messages_resumes_after_yield(self) -> None:
        from post.mail.search import SearchScanCursor

        query = parse_search_query("needle")
        assert query is not None
        messages = [
            {
                "uid": str(index),
                "subject": f"needle {index}",
                "flags": {"seen": True},
            }
            for index in range(5)
        ]
        cursor = SearchScanCursor()
        yield_checks = 0

        def should_yield() -> bool:
            nonlocal yield_checks
            yield_checks += 1
            return yield_checks >= 3

        first = filter_messages_by_query(
            messages,
            query,
            cursor=cursor,
            should_yield=should_yield,
        )
        self.assertEqual([message["uid"] for message in first], ["0", "1"])
        self.assertEqual(cursor.index, 2)

        second = filter_messages_by_query(
            messages,
            query,
            cursor=cursor,
        )
        self.assertEqual([message["uid"] for message in second], ["2", "3", "4"])
        self.assertEqual(cursor.index, 5)


class FilterSearchMatchesForFolderTests(unittest.TestCase):
    def test_keeps_matches_for_requested_folder(self) -> None:
        from post.mail.search import (
            annotate_search_match,
            filter_search_matches_for_folder,
        )

        inbox_match = annotate_search_match(
            {"uid": "1", "subject": "a"},
            account_uid="acct-1",
            folder_name="INBOX",
        )
        sent_match = annotate_search_match(
            {"uid": "2", "subject": "b"},
            account_uid="acct-1",
            folder_name="Sent",
        )
        other_account = annotate_search_match(
            {"uid": "3", "subject": "c"},
            account_uid="acct-2",
            folder_name="INBOX",
        )
        filtered = filter_search_matches_for_folder(
            [inbox_match, sent_match, other_account],
            account_uid="acct-1",
            folder_name="INBOX",
        )
        self.assertEqual([message["uid"] for message in filtered], ["1"])


class GroupListKeysByLocationTests(unittest.TestCase):
    def test_groups_search_keys_by_folder(self) -> None:
        from post.mail.search import (
            annotate_search_match,
            group_list_keys_by_location,
            make_search_row_key,
        )

        inbox = annotate_search_match(
            {"uid": "1"}, account_uid="acct-1", folder_name="INBOX"
        )
        sent = annotate_search_match(
            {"uid": "2"}, account_uid="acct-1", folder_name="Sent"
        )
        inbox2 = annotate_search_match(
            {"uid": "3"}, account_uid="acct-1", folder_name="INBOX"
        )
        locations = {
            inbox["_search_row_key"]: ("acct-1", "INBOX", "1"),
            sent["_search_row_key"]: ("acct-1", "Sent", "2"),
            inbox2["_search_row_key"]: ("acct-1", "INBOX", "3"),
        }

        groups = group_list_keys_by_location(
            [
                inbox["_search_row_key"],
                sent["_search_row_key"],
                inbox2["_search_row_key"],
                "missing",
            ],
            locations.get,
        )
        self.assertEqual(
            groups[("acct-1", "INBOX")],
            [
                (make_search_row_key("acct-1", "INBOX", "1"), "1"),
                (make_search_row_key("acct-1", "INBOX", "3"), "3"),
            ],
        )
        self.assertEqual(
            groups[("acct-1", "Sent")],
            [(make_search_row_key("acct-1", "Sent", "2"), "2")],
        )

    def test_parse_search_row_key(self) -> None:
        from post.mail.search import make_search_row_key, parse_search_row_key

        key = make_search_row_key("acct", "Sent", "42")
        self.assertEqual(parse_search_row_key(key), ("acct", "Sent", "42"))
        self.assertIsNone(parse_search_row_key("plain-uid"))
        self.assertIsNone(parse_search_row_key("a\0b"))


if __name__ == "__main__":
    unittest.main()
