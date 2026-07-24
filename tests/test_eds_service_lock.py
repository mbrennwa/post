# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""MailService must not hold ``_lock`` across Camel connect / password prompts."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from post.mail.eds import MailService


class ServiceLockReleaseTests(unittest.TestCase):
    def test_call_without_service_lock_releases_nested_lock(self) -> None:
        service = MailService(registry=mock.Mock())
        seen_depth: list[int] = []

        def probe() -> str:
            seen_depth.append(service._lock._recursion_count())
            return "ok"

        with service._lock:
            with service._lock:
                result = service._call_without_service_lock(probe)

        self.assertEqual(result, "ok")
        self.assertEqual(seen_depth, [0])
        self.assertEqual(service._lock._recursion_count(), 0)

    def test_get_store_releases_lock_during_connect(self) -> None:
        service = MailService(registry=mock.Mock())
        store = mock.Mock()
        store.get_connection_status.return_value = mock.Mock()
        # Force the create path.
        service._stores = {}

        source = mock.Mock()
        mail_ext = mock.Mock()
        mail_ext.get_backend_name.return_value = "imapx"
        source.get_extension.return_value = mail_ext
        source.camel_configure_service = mock.Mock()
        service.registry.ref_source.return_value = source

        session = mock.Mock()
        session.add_service.return_value = store
        service._ensure_session = mock.Mock(return_value=session)
        service._prepare_account_credentials_unlocked = mock.Mock()
        service._configure_store_settings_unlocked = mock.Mock()

        lock_held_during_sync: list[bool] = []

        def fake_sync(_store, _account_uid=None, *, cancellable=None) -> None:
            lock_held_during_sync.append(service._lock._recursion_count() > 0)

        service._sync_store_online_state_unlocked = fake_sync

        with service._lock:
            result = service._get_store_unlocked("acct-1")

        self.assertIs(result, store)
        self.assertEqual(lock_held_during_sync, [False])
        self.assertIs(service._stores["acct-1"], store)

    def test_preempt_background_work_cancels_folder_list(self) -> None:
        service = MailService(registry=mock.Mock())
        service.cancel_folder_search = mock.Mock()
        service.cancel_folder_list = mock.Mock()
        service.offline_sync.cancel_all = mock.Mock()
        service._sync_setup_cancel = None

        service._preempt_background_work()

        service.cancel_folder_search.assert_called_once()
        service.cancel_folder_list.assert_called_once()
        service.offline_sync.cancel_all.assert_called_once()

    def test_folder_list_register_keeps_sibling_cancellables(self) -> None:
        service = MailService(registry=mock.Mock())
        first = mock.Mock()
        second = mock.Mock()
        first.cancel = mock.Mock()
        second.cancel = mock.Mock()

        service._register_folder_list_cancellable(first)
        service._register_folder_list_cancellable(second)
        first.cancel.assert_not_called()
        second.cancel.assert_not_called()
        self.assertEqual(service._folder_list_cancellables, {first, second})

        service._unregister_folder_list_cancellable(first)
        self.assertEqual(service._folder_list_cancellables, {second})

        service.cancel_folder_list()
        second.cancel.assert_called_once()
        self.assertEqual(service._folder_list_cancellables, set())

    def test_set_account_user_online_does_not_block_caller(self) -> None:
        service = MailService(registry=mock.Mock())
        io = mock.Mock()
        with (
            mock.patch("post.mail.eds.is_mail_io_thread", return_value=False),
            mock.patch("post.mail.eds.get_mail_io_thread", return_value=io),
            mock.patch("post.preferences.set_account_user_online") as save_pref,
        ):
            service.set_account_user_online("acct-1", False)

        save_pref.assert_called_once_with("acct-1", False)
        io.submit_front.assert_called_once()
        args = io.submit_front.call_args[0]
        self.assertEqual(args[0], service._apply_account_user_online_unlocked)
        self.assertEqual(args[1], "acct-1")
        io.run_sync.assert_not_called()

    def test_sync_store_online_respects_user_offline(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        store = mock.Mock()
        store.connect_sync = mock.Mock()
        store.set_online_sync = mock.Mock()

        with mock.patch(
            "post.mail.eds.get_account_user_online", return_value=False
        ):
            # Non-OfflineStore: must not connect when the user took the account offline.
            service._sync_store_online_state_unlocked(store, "acct-1")
        store.connect_sync.assert_not_called()
        store.set_online_sync.assert_not_called()

        with mock.patch(
            "post.mail.eds.get_account_user_online", return_value=True
        ):
            service._sync_store_online_state_unlocked(store, "acct-1")
        store.connect_sync.assert_called_once()

    def test_get_account_does_not_hold_lock_across_list_accounts(self) -> None:
        service = MailService(registry=mock.Mock())
        account = mock.Mock(uid="acct-1")
        listed = threading.Event()
        can_finish = threading.Event()

        def slow_list_accounts():
            listed.set()
            can_finish.wait(timeout=2)
            service._accounts_by_uid = {"acct-1": account}
            return [account]

        service.list_accounts = slow_list_accounts  # type: ignore[method-assign]
        service._accounts_by_uid = {}

        result_box: list[object] = []

        def caller() -> None:
            result_box.append(service.get_account("acct-1"))

        thread = threading.Thread(target=caller)
        thread.start()
        self.assertTrue(listed.wait(timeout=2))
        # Main thread can take the lock while list_accounts runs.
        acquired = service._lock.acquire(timeout=1)
        self.assertTrue(acquired)
        service._lock.release()
        can_finish.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box, [account])


if __name__ == "__main__":
    unittest.main()
