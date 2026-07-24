# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class ListMessagesPageDispatchTests(unittest.TestCase):
    @mock.patch("post.mail.eds.is_mail_io_thread", return_value=False)
    @mock.patch("post.mail.eds.get_mail_io_thread")
    def test_dispatches_to_mail_thread(self, get_io_thread, _is_mail_io) -> None:
        io_thread = mock.Mock()
        io_thread.run_sync.return_value = ([], 0, 0, False)
        get_io_thread.return_value = io_thread

        service = MailService(registry=mock.Mock())
        service.list_messages_page(
            "acct-1",
            "INBOX",
            offset=10,
            limit=25,
            sync=True,
        )

        io_thread.run_sync.assert_called_once_with(
            service._list_messages_page_unlocked,
            "acct-1",
            "INBOX",
            offset=10,
            limit=25,
            sync=True,
        )

    @mock.patch("post.mail.eds.is_mail_io_thread", return_value=True)
    def test_runs_inline_on_mail_thread(self, _is_mail_io) -> None:
        service = MailService(registry=mock.Mock())
        expected = ([{"uid": "1"}], 1, 1, False)

        with mock.patch.object(
            service,
            "_list_messages_page_unlocked",
            return_value=expected,
        ) as unlocked:
            result = service.list_messages_page("acct-1", "INBOX")

        unlocked.assert_called_once_with(
            "acct-1",
            "INBOX",
            offset=0,
            limit=50,
            sync=True,
        )
        self.assertEqual(result, expected)


class ListFoldersDispatchTests(unittest.TestCase):
    @mock.patch("post.mail.eds.is_mail_io_thread", return_value=False)
    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_dispatches_to_mail_thread(self, run_on_mail_thread, _is_mail_io) -> None:
        run_on_mail_thread.return_value = [{"full_name": "INBOX"}]
        service = MailService(registry=mock.Mock())

        result = service.list_folders("acct-1")

        run_on_mail_thread.assert_called_once_with(
            service._list_folders_unlocked,
            "acct-1",
            cancellable=None,
        )
        self.assertEqual(result, [{"full_name": "INBOX"}])

    @mock.patch("post.mail.eds.is_mail_io_thread", return_value=True)
    def test_runs_inline_on_mail_thread(self, _is_mail_io) -> None:
        service = MailService(registry=mock.Mock())
        expected = [{"full_name": "INBOX"}]

        with mock.patch.object(
            service,
            "_list_folders_unlocked",
            return_value=expected,
        ) as unlocked:
            result = service.list_folders("acct-1")

        unlocked.assert_called_once_with("acct-1", cancellable=None)
        self.assertEqual(result, expected)


class ReadPathDispatchTests(unittest.TestCase):
    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_read_message_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = {"uid": "1"}
        service = MailService(registry=mock.Mock())

        result = service.read_message("acct-1", "INBOX", "42", mark_seen=False)

        run_on_mail_thread.assert_called_once_with(
            service._read_message_unlocked,
            "acct-1",
            "INBOX",
            "42",
            mark_seen=False,
        )
        self.assertEqual(result, {"uid": "1"})

    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_get_folder_stats_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = (3, 10)
        service = MailService(registry=mock.Mock())

        result = service.get_folder_stats("acct-1", "INBOX")

        run_on_mail_thread.assert_called_once_with(
            service._get_folder_stats_unlocked,
            "acct-1",
            "INBOX",
        )
        self.assertEqual(result, (3, 10))

    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_get_account_folder_stats_uses_mail_thread(
        self, run_on_mail_thread
    ) -> None:
        run_on_mail_thread.return_value = {"INBOX": (1, 2)}
        service = MailService(registry=mock.Mock())

        result = service.get_account_folder_stats("acct-1")

        run_on_mail_thread.assert_called_once_with(
            service._get_account_folder_stats_unlocked,
            "acct-1",
        )
        self.assertEqual(result, {"INBOX": (1, 2)})

    def test_account_folder_stats_falls_back_when_refresh_returns_none(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        service._folder_tree_cache["acct-1"] = [
            {"full_name": "INBOX", "unread": 4, "total": 9},
            {"full_name": "Sent", "unread": 0, "total": 2},
        ]
        store = mock.Mock()
        store.get_folder_info_sync.return_value = None

        with mock.patch.object(service, "_get_store_unlocked", return_value=store):
            stats = service._get_account_folder_stats_unlocked("acct-1")

        self.assertEqual(stats, {"INBOX": (4, 9), "Sent": (0, 2)})
        store.get_folder_info_sync.assert_called_once()
        cancellable = store.get_folder_info_sync.call_args.args[2]
        self.assertIsNotNone(cancellable)

    def test_account_folder_stats_falls_back_when_refresh_cancelled(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        service = MailService(registry=mock.Mock())
        service._network_available = True
        service._folder_tree_cache["acct-1"] = [
            {"full_name": "INBOX", "unread": 1, "total": 3},
        ]
        store = mock.Mock()
        store.get_folder_info_sync.side_effect = GLib.Error.new_literal(
            Gio.io_error_quark(),
            "Operation was cancelled",
            Gio.IOErrorEnum.CANCELLED,
        )

        with mock.patch.object(service, "_get_store_unlocked", return_value=store):
            stats = service._get_account_folder_stats_unlocked("acct-1")

        self.assertEqual(stats, {"INBOX": (1, 3)})


class FolderStatsOfflineFallbackTests(unittest.TestCase):
    def test_returns_memory_index_when_offline(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = MailService(registry=mock.Mock())
        service._network_available = False
        service._folder_indexes[("acct-1", "INBOX")] = _FolderMessageIndex(
            messages=[{"uid": "1"}],
            unread=2,
            total=5,
        )
        folder = mock.Mock()
        store = mock.Mock()
        store.get_folder_sync.return_value = folder

        with mock.patch.object(service, "_get_store_unlocked", return_value=store):
            unread, total = service._get_folder_stats_unlocked("acct-1", "INBOX")

        self.assertEqual((unread, total), (2, 5))
        folder.refresh_info_sync.assert_not_called()

    def test_falls_back_to_disk_cache_on_network_error(self) -> None:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        service = MailService(registry=mock.Mock())
        service._network_available = True
        folder = mock.Mock()
        folder.refresh_info_sync.side_effect = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"),
            "Network is unreachable",
            39,
        )
        store = mock.Mock()
        store.get_folder_sync.return_value = folder

        with (
            mock.patch.object(service, "_get_store_unlocked", return_value=store),
            mock.patch(
                "post.mail.eds.folder_index_cache.load",
                return_value=([], 1, 3),
            ),
        ):
            unread, total = service._get_folder_stats_unlocked("acct-1", "INBOX")

        self.assertEqual((unread, total), (1, 3))


class InboxFolderNameCachedTests(unittest.TestCase):
    def test_returns_none_when_folder_tree_not_cached(self) -> None:
        service = MailService(registry=mock.Mock())
        self.assertIsNone(service.get_inbox_folder_name_cached("acct-1"))

    def test_returns_guessed_inbox_from_cache(self) -> None:
        service = MailService(registry=mock.Mock())
        service._folder_tree_cache["acct-1"] = [
            {"full_name": "Archive", "display_name": "Archive"},
            {"full_name": "INBOX", "display_name": "Inbox"},
        ]
        self.assertEqual(service.get_inbox_folder_name_cached("acct-1"), "INBOX")

    def test_guesses_inbox_by_display_name_when_full_name_differs(self) -> None:
        service = MailService(registry=mock.Mock())
        service._folder_tree_cache["acct-1"] = [
            {"full_name": "AQMkAD...", "display_name": "Inbox"},
        ]
        self.assertEqual(service.get_inbox_folder_name_cached("acct-1"), "AQMkAD...")
