# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel offline settings mapping."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.offline_settings import (
    apply_offline_settings_to_store,
    downsync_expression_for_mode,
)
from post.mail.search import (
    MessageSearchQuery,
    SearchTerm,
    parse_search_query,
    query_to_sexp,
)
from post.preferences import (
    OFFLINE_BODY_SYNC_ALL,
    OFFLINE_BODY_SYNC_LAST_MONTH,
    OFFLINE_BODY_SYNC_LAST_YEAR,
    OFFLINE_BODY_SYNC_OFF,
)


class DownsyncExpressionTests(unittest.TestCase):
    def test_off_returns_none(self) -> None:
        self.assertIsNone(downsync_expression_for_mode(OFFLINE_BODY_SYNC_OFF))

    def test_enabled_modes_use_match_all(self) -> None:
        self.assertEqual(
            downsync_expression_for_mode(OFFLINE_BODY_SYNC_ALL),
            "(match-all)",
        )


class ApplyOfflineSettingsTests(unittest.TestCase):
    def test_off_disables_stay_synchronized(self) -> None:
        store = mock.Mock()
        settings = mock.Mock()
        store.ref_settings.return_value = settings
        with mock.patch(
            "post.mail.offline_settings.isinstance",
            return_value=True,
        ):
            apply_offline_settings_to_store(
                store, "acct-1", mode=OFFLINE_BODY_SYNC_OFF
            )
        settings.set_stay_synchronized.assert_called_once_with(False)

    def test_last_month_sets_age_limit(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        store = mock.Mock()
        settings = mock.Mock(spec=Camel.OfflineSettings)
        store.ref_settings.return_value = settings
        with mock.patch(
            "post.mail.offline_settings.get_account_offline_body_sync",
            return_value=OFFLINE_BODY_SYNC_LAST_MONTH,
        ):
            with mock.patch(
                "post.mail.offline_settings.isinstance",
                return_value=True,
            ):
                apply_offline_settings_to_store(store, "acct-1")
        settings.set_stay_synchronized.assert_called_once_with(True)
        settings.set_limit_by_age.assert_called_once_with(True)
        settings.set_limit_unit.assert_called_once_with(Camel.TimeUnit.MONTHS)
        settings.set_limit_value.assert_called_once_with(1)

    def test_all_disables_age_limit(self) -> None:
        store = mock.Mock()
        settings = mock.Mock()
        store.ref_settings.return_value = settings
        with mock.patch(
            "post.mail.offline_settings.get_account_offline_body_sync",
            return_value=OFFLINE_BODY_SYNC_ALL,
        ):
            with mock.patch(
                "post.mail.offline_settings.isinstance",
                return_value=True,
            ):
                apply_offline_settings_to_store(store, "acct-1")
        settings.set_limit_by_age.assert_called_once_with(False)


class QueryToSexpTests(unittest.TestCase):
    def test_from_prefix(self) -> None:
        query = parse_search_query("from:alice")
        assert query is not None
        self.assertEqual(
            query_to_sexp(query),
            '(match-all (header-contains "From" "alice"))',
        )

    def test_bare_text_includes_body(self) -> None:
        query = parse_search_query("invoice")
        assert query is not None
        sexp = query_to_sexp(query)
        self.assertIn("body-contains", sexp)
        self.assertIn("header-contains", sexp)
        self.assertIn("invoice", sexp)

    def test_boolean_read(self) -> None:
        query = MessageSearchQuery(terms=(SearchTerm(field="read", negated=True),))
        self.assertEqual(
            query_to_sexp(query),
            '(match-all (not (system-flag "seen")))',
        )

    def test_combined_terms_use_and(self) -> None:
        query = parse_search_query("from:alice subject:invoice")
        assert query is not None
        sexp = query_to_sexp(query)
        self.assertIn("(and ", sexp)
        self.assertIn('header-contains "From" "alice"', sexp)
        self.assertIn('header-contains "Subject" "invoice"', sexp)


if __name__ == "__main__":
    unittest.main()
