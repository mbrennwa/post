# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel offline settings mapping."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.offline_settings import (
    account_is_user_offline,
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
    set_account_user_online,
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


class RefreshOfflineSettingsTests(unittest.TestCase):
    def test_refresh_submits_to_mail_io_thread(self) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=mock.Mock())
        service._stores = {"acct-1": mock.Mock()}

        with mock.patch("post.mail.eds.get_mail_io_thread") as get_io:
            io_thread = mock.Mock()
            get_io.return_value = io_thread
            service.refresh_offline_settings("acct-1")
            io_thread.submit.assert_called_once_with(
                service._refresh_offline_settings_unlocked, "acct-1"
            )

    @mock.patch("post.mail.eds.apply_offline_settings_to_store")
    @mock.patch("post.mail.eds.MailService._get_store_unlocked")
    @mock.patch("post.mail.eds.MailService.schedule_offline_body_sync")
    def test_refresh_opens_store_when_not_cached(
        self,
        schedule_sync: mock.Mock,
        get_store: mock.Mock,
        apply_settings: mock.Mock,
    ) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=mock.Mock())
        store = mock.Mock()
        get_store.return_value = store

        service._refresh_offline_settings_unlocked("acct-1")

        get_store.assert_called_once_with("acct-1")
        apply_settings.assert_called_once_with(store, "acct-1")
        schedule_sync.assert_called_once_with("acct-1")


class OfflineSyncYieldTests(unittest.TestCase):
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    def test_run_account_sync_yields_when_interactive_work_pending(
        self, get_io: mock.Mock,
    ) -> None:
        from post.mail.offline_sync import OfflineBodySyncCoordinator

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = True
        get_io.return_value = io_thread

        mail = mock.Mock()
        mail.get_account.return_value = mock.Mock(display_label="Test")
        coordinator = OfflineBodySyncCoordinator(mail)

        folder = mock.Mock()
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = False

        complete = coordinator._run_account_sync(
            "acct-1",
            "last_month",
            cancellable,
            folders=[folder],
            folder_index=0,
        )

        self.assertFalse(complete)
        io_thread.submit_background.assert_called_once()


class ShutdownSyncTests(unittest.TestCase):
    def test_shutdown_cancels_active_offline_sync_without_blocking(self) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=mock.Mock())
        coordinator = mock.Mock()
        coordinator.is_active.return_value = True
        service._offline_sync = coordinator

        with mock.patch.object(service, "wait_for_outbound_sends"):
            with mock.patch.object(service, "wait_for_pending_mail_ops") as wait_ops:
                with mock.patch("post.mail.eds.get_mail_io_thread") as get_io:
                    io_thread = mock.Mock()
                    get_io.return_value = io_thread
                    with mock.patch("post.mail.eds.run_on_mail_thread") as run_sync:
                        service.shutdown_sync()

        coordinator.cancel_all.assert_called_once()
        wait_ops.assert_called_once_with(timeout=2.0)
        io_thread.submit_background.assert_called_once_with(service._flush_stores_on_shutdown)
        run_sync.assert_not_called()

    def test_shutdown_flushes_in_background_when_offline_sync_idle(self) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=mock.Mock())
        coordinator = mock.Mock()
        coordinator.is_active.return_value = False
        service._offline_sync = coordinator

        with mock.patch.object(service, "wait_for_outbound_sends"):
            with mock.patch.object(service, "cancel_folder_search") as cancel_search:
                with mock.patch.object(service, "wait_for_pending_mail_ops") as wait_ops:
                    with mock.patch("post.mail.eds.get_mail_io_thread") as get_io:
                        io_thread = mock.Mock()
                        get_io.return_value = io_thread
                        with mock.patch("post.mail.eds.run_on_mail_thread") as run_sync:
                            service.shutdown_sync()

        cancel_search.assert_called_once_with()
        coordinator.cancel_all.assert_called_once()
        wait_ops.assert_called_once_with(timeout=1.0)
        io_thread.submit_background.assert_called_once_with(
            service._flush_stores_on_shutdown
        )
        run_sync.assert_not_called()


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


class AccountUserOfflineTests(unittest.TestCase):
    def test_account_is_user_offline_reads_preferences(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                self.assertFalse(account_is_user_offline("acct-1"))
                set_account_user_online("acct-1", False)
                self.assertTrue(account_is_user_offline("acct-1"))


if __name__ == "__main__":
    unittest.main()
