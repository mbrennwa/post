# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""A GOA Camel session is discarded when its credentials change (#519)."""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import MagicMock

from post.mail.auth import goa_attention_should_confirm
from post.mail.camel_runtime import AccountCamelRuntime
from post.mail.eds import MailService
from post.mail.offline_sync import OfflineBodySyncCoordinator
from post.preferences import OFFLINE_BODY_SYNC_ALL
from post.sidebar import MailSidebar
from post.window import MainWindow


class AccountMayContactServerTests(unittest.TestCase):
    def test_closed_for_network_user_offline_and_sign_in(self) -> None:
        service = MailService(registry=MagicMock())
        self.assertTrue(service.account_may_contact_server("acct-1"))

        service._network_available = False
        self.assertFalse(service.account_may_contact_server("acct-1"))

        service._network_available = True
        with mock.patch(
            "post.mail.eds.get_account_user_online", return_value=False
        ):
            self.assertFalse(service.account_may_contact_server("acct-1"))

        service.set_account_connect_health("acct-1", "needs_sign_in")
        self.assertFalse(service.account_may_contact_server("acct-1"))

    def test_needs_sign_in_discards_a_running_helper(self) -> None:
        service = MailService(registry=MagicMock())
        service._running_helper = lambda _uid: object()  # type: ignore[method-assign]
        service.invalidate_account_connection = MagicMock()
        service.set_account_connect_health("acct-1", "needs_sign_in")
        service.invalidate_account_connection.assert_called_once_with("acct-1")
        service.invalidate_account_connection.reset_mock()
        service.set_account_connect_health("acct-1", "needs_sign_in")
        service.invalidate_account_connection.assert_not_called()


class LocalReadGateTests(unittest.TestCase):
    def _read(self, service: MailService) -> None:
        folder = MagicMock()
        cached = MagicMock(name="cached")
        cached.get_header.return_value = None
        cached.get_message_id.return_value = None
        folder.get_message_cached.return_value = cached
        folder.get_message_sync.side_effect = AssertionError(
            "get_message_sync must not run while the gate is closed"
        )
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        service._get_store_unlocked = MagicMock(return_value=store)  # type: ignore[method-assign]
        service._mark_message_seen_unlocked = MagicMock(  # type: ignore[method-assign]
            return_value=(0, 1)
        )
        with (
            mock.patch(
                "post.mail.helpers.message_info_to_dict",
                return_value={"uid": "42"},
            ),
            mock.patch("post.mail.helpers.enrich_message_dict_from_mime"),
            mock.patch(
                "post.mail.helpers.extract_message_bodies",
                return_value={"plain": "Hello", "html": None},
            ),
            mock.patch("post.mail.helpers.extract_attachments", return_value=[]),
            mock.patch("post.mail.helpers.extract_inline_images", return_value=[]),
            mock.patch(
                "post.mail.helpers.has_visible_attachments", return_value=False
            ),
        ):
            result = service._read_message_unlocked("acct-1", "INBOX", "42")
        self.assertEqual(result["body_plain"], "Hello")
        folder.get_message_sync.assert_not_called()
        service._mark_message_seen_unlocked.assert_called_once()
        service._get_store_unlocked.assert_called()
        self.assertFalse(
            service._get_store_unlocked.call_args.kwargs.get("allow_online", True)
        )

    def test_network_down_reads_cache(self) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = False
        self._read(service)

    def test_user_offline_reads_cache(self) -> None:
        service = MailService(registry=MagicMock())
        with mock.patch(
            "post.mail.eds.get_account_user_online", return_value=False
        ):
            self._read(service)

    def test_needs_sign_in_reads_cache(self) -> None:
        service = MailService(registry=MagicMock())
        service.set_account_connect_health("acct-1", "needs_sign_in")
        self._read(service)

    def test_read_timeout_does_not_set_needs_sign_in(self) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        folder.get_message_cached.return_value = None
        service._get_message_sync_with_timeout = MagicMock(  # type: ignore[method-assign]
            side_effect=TimeoutError("timed out loading message")
        )
        with self.assertRaises(TimeoutError):
            service._get_message_mime_sync(folder, "acct-1", "INBOX", "42")
        self.assertEqual(service.get_account_connect_health("acct-1"), "ok")


class StopOnlineWorkTests(unittest.TestCase):
    @mock.patch(
        "post.mail.offline_sync.effective_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    def test_schedule_account_skips_needs_sign_in(
        self, _offline: mock.Mock, _mode: mock.Mock
    ) -> None:
        mail = mock.Mock()
        mail.is_network_available.return_value = True
        mail.get_account_connect_health.return_value = "needs_sign_in"
        mail.account_may_contact_server.return_value = False
        coordinator = OfflineBodySyncCoordinator(mail)
        coordinator.schedule_account("acct-1")
        mail.submit_background.assert_not_called()
        self.assertNotIn("acct-1", coordinator._running)

    def test_folder_count_poll_skips_closed_gate(self) -> None:
        sidebar = mock.Mock()
        sidebar._folder_count_poll_generation = 1
        sidebar._mail.account_may_contact_server.return_value = False
        sidebar._mail.has_interactive_work_pending.return_value = False

        def run_job(_name: str, worker, **_kwargs: object) -> None:
            worker()

        sidebar._mail.submit_job.side_effect = run_job
        with mock.patch("post.sidebar.schedule_on_gtk_main") as schedule:
            MailSidebar._poll_account_folder_counts_at(sidebar, 1, ["acct-1"], 0)
        sidebar._mail.get_account_folder_stats_via_graph.assert_not_called()
        sidebar._mail.get_account_folder_stats.assert_not_called()
        schedule.assert_called_once_with(
            sidebar._poll_account_folder_counts_at, 1, ["acct-1"], 1
        )

    def test_reconnect_discards_the_helper_session(self) -> None:
        window = mock.Mock()
        with mock.patch("post.window.GLib.idle_add"):
            MainWindow._reconnect_account_after_goa_reauth(window, "acct-1")
            work = window._mail.submit_front.call_args.args[1]
            work()
        window._mail.invalidate_account_connection.assert_called_once_with("acct-1")
        window._mail._invalidate_account_connection_unlocked.assert_not_called()


class GoaAttentionTests(unittest.TestCase):
    def test_baseline_false_is_not_a_transition(self) -> None:
        self.assertFalse(goa_attention_should_confirm(None, False))
        self.assertTrue(goa_attention_should_confirm(None, True))
        self.assertTrue(goa_attention_should_confirm(True, False))
        self.assertFalse(goa_attention_should_confirm(False, False))

    def _service(self) -> MailService:
        service = MailService(registry=MagicMock())
        service._account_uid_for_goa_id = lambda _goa_id: "acct-1"  # type: ignore[method-assign]
        service.invalidate_account_connection = MagicMock()  # type: ignore[method-assign]
        service.schedule_offline_body_sync = MagicMock()  # type: ignore[method-assign]
        service.schedule_operation_queue_flush = MagicMock()  # type: ignore[method-assign]
        return service

    def test_clearing_discards_helper_only_when_credentials_are_ok(self) -> None:
        service = self._service()
        with (
            mock.patch("post.mail.eds.source_uses_goa", return_value=True),
            mock.patch(
                "post.mail.eds.ensure_goa_credentials", return_value="failed"
            ) as ensure,
        ):
            service.handle_goa_attention("goa-1", False)
        ensure.assert_called_once()
        service.invalidate_account_connection.assert_not_called()
        self.assertEqual(
            service.get_account_connect_health("acct-1"), "needs_sign_in"
        )
        service.schedule_offline_body_sync.assert_not_called()

        service.invalidate_account_connection.reset_mock()
        with (
            mock.patch("post.mail.eds.source_uses_goa", return_value=True),
            mock.patch(
                "post.mail.eds.ensure_goa_credentials", return_value="ok"
            ) as ensure_ok,
        ):
            service.handle_goa_attention("goa-1", False)
        ensure_ok.assert_called_once()
        service.invalidate_account_connection.assert_called_once_with("acct-1")
        self.assertEqual(service.get_account_connect_health("acct-1"), "ok")
        service.schedule_offline_body_sync.assert_called_once_with("acct-1")
        service.schedule_operation_queue_flush.assert_called_once_with("acct-1")

    def test_cancelled_confirmation_leaves_the_session(self) -> None:
        service = self._service()
        with (
            mock.patch("post.mail.eds.source_uses_goa", return_value=True),
            mock.patch(
                "post.mail.eds.ensure_goa_credentials", return_value="cancelled"
            ),
        ):
            service.handle_goa_attention("goa-1", True)
        service.invalidate_account_connection.assert_not_called()
        self.assertEqual(service.get_account_connect_health("acct-1"), "ok")

    def test_failure_discards_a_running_helper(self) -> None:
        service = self._service()
        service._running_helper = lambda _uid: object()  # type: ignore[method-assign]
        with (
            mock.patch("post.mail.eds.source_uses_goa", return_value=True),
            mock.patch(
                "post.mail.eds.ensure_goa_credentials", return_value="failed"
            ),
        ):
            service.handle_goa_attention("goa-1", True)
        service.invalidate_account_connection.assert_called_once_with("acct-1")
        self.assertEqual(
            service.get_account_connect_health("acct-1"), "needs_sign_in"
        )


class HelperSeedTests(unittest.TestCase):
    def test_new_helper_is_seeded_once_before_other_calls(self) -> None:
        runtime = AccountCamelRuntime(account_uid="acct-1")
        seeded: list[str] = []
        runtime.set_started_callback(seeded.append)

        def start() -> None:
            runtime._proc = MagicMock()
            runtime._proc.poll.return_value = None
            runtime._alive = True

        runtime._start_unlocked = start  # type: ignore[method-assign]
        runtime.ensure_started()
        runtime.ensure_started()
        self.assertEqual(seeded, ["acct-1"])
