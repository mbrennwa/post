# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Network reconnect must not run_sync from GTK (#400)."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Camel", "1.2")
gi.require_version("GLib", "2.0")
from gi.repository import Camel, GLib

from post.mail.eds import MailService


class NetworkReconnectDispatchTests(unittest.TestCase):
    def test_set_network_available_submits_front_not_run_sync(self) -> None:
        service = MailService(registry=mock.Mock())
        service._set_network_available_unlocked = mock.Mock()
        io = mock.Mock()
        with mock.patch("post.mail.eds.is_mail_io_thread", return_value=False):
            with mock.patch(
                "post.mail.eds.get_mail_io_thread", return_value=io
            ):
                service.set_network_available(True)

        io.submit_front.assert_called_once()
        io.run_sync.assert_not_called()
        service._set_network_available_unlocked.assert_not_called()
        work = io.submit_front.call_args.args[0]
        work()
        service._set_network_available_unlocked.assert_called_once_with(True)

    def test_go_online_sync_submits_front_not_run_sync(self) -> None:
        service = MailService(registry=mock.Mock())
        service._go_online_sync_unlocked = mock.Mock()
        io = mock.Mock()
        with mock.patch("post.mail.eds.is_mail_io_thread", return_value=False):
            with mock.patch(
                "post.mail.eds.get_mail_io_thread", return_value=io
            ):
                service.go_online_sync()

        io.submit_front.assert_called_once()
        io.run_sync.assert_not_called()
        work = io.submit_front.call_args.args[0]
        work()
        service._go_online_sync_unlocked.assert_called_once_with()

    def test_set_network_available_on_complete_idle(self) -> None:
        service = MailService(registry=mock.Mock())
        service._set_network_available_unlocked = mock.Mock()
        done = mock.Mock()
        with mock.patch("post.mail.eds.is_mail_io_thread", return_value=True):
            with mock.patch("post.mail.eds.GLib.idle_add") as idle_add:
                service.set_network_available(False, on_complete=done)

        service._set_network_available_unlocked.assert_called_once_with(False)
        idle_add.assert_called_once()
        self.assertIs(idle_add.call_args.args[1], done)


class NetworkReconnectStorePassTests(unittest.TestCase):
    def test_store_timeout_wrapper_continues_after_failure(self) -> None:
        service = MailService(registry=mock.Mock())
        calls: list[str] = []

        def sync_one(store, account_uid, *, cancellable=None) -> None:
            calls.append(account_uid)
            if account_uid == "bad":
                raise GLib.Error.new_literal(
                    GLib.quark_from_string("g-io-error-quark"),
                    "auth failed",
                    0,
                )

        service._sync_store_online_state_unlocked = sync_one  # type: ignore[method-assign]
        service._sync_store_online_with_timeout_unlocked(mock.Mock(), "bad")
        service._sync_store_online_with_timeout_unlocked(mock.Mock(), "good")
        self.assertEqual(calls, ["bad", "good"])

    def test_set_network_available_unlocked_clears_indexes_on_online(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = False
        service._session = mock.Mock()
        service._folder_indexes[("acct", "Archive")] = mock.Mock()
        synced: list[str] = []

        def sync_one(store, account_uid) -> None:
            synced.append(account_uid)

        service._sync_store_online_with_timeout_unlocked = sync_one  # type: ignore[method-assign]
        offline = mock.create_autospec(Camel.OfflineStore, instance=True)
        service._stores = {"acct-1": offline}

        with mock.patch.object(
            service.offline_sync, "schedule_all_accounts"
        ) as schedule_all:
            service._set_network_available_unlocked(True)

        self.assertTrue(service._network_available)
        self.assertEqual(service._folder_indexes, {})
        self.assertEqual(synced, ["acct-1"])
        schedule_all.assert_called_once()
        service._session.set_online.assert_called_once_with(True)

    def test_set_network_available_unlocked_noop_when_unchanged(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        service._sync_store_online_with_timeout_unlocked = mock.Mock()
        service._set_network_available_unlocked(True)
        service._sync_store_online_with_timeout_unlocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
