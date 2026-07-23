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

        def fake_sync(_store, *, cancellable=None) -> None:
            lock_held_during_sync.append(service._lock._recursion_count() > 0)

        service._sync_store_online_state_unlocked = fake_sync

        with service._lock:
            result = service._get_store_unlocked("acct-1")

        self.assertIs(result, store)
        self.assertEqual(lock_held_during_sync, [False])
        self.assertIs(service._stores["acct-1"], store)

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
