# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class DraftDispatchTests(unittest.TestCase):
    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_save_draft_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = ("Drafts", "7")
        service = MailService(registry=mock.Mock())

        result = service.save_draft(
            "acct-1",
            subject="Hi",
            body="Body",
        )

        run_on_mail_thread.assert_called_once()
        self.assertEqual(
            run_on_mail_thread.call_args.args[0].__name__,
            "_save_draft_unlocked",
        )
        self.assertEqual(result, ("Drafts", "7"))

    def test_save_draft_unlocked_appends_locally(self) -> None:
        service = MailService(registry=mock.Mock())
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.email = "user@example.com"
        service.get_account = mock.Mock(return_value=account)
        service._drafts_folder_name_unlocked = mock.Mock(return_value="Drafts")
        service._append_draft_unlocked = mock.Mock(return_value="draft-1")

        with mock.patch(
            "post.mail.eds.build_draft_mime_message",
            return_value=mock.Mock(),
        ):
            folder_name, uid = service._save_draft_unlocked(
                "acct-1",
                to=["bob@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
                body_html=None,
                in_reply_to=None,
                references=None,
                existing_uid=None,
                drafts_folder_name=None,
            )

        service._append_draft_unlocked.assert_called_once()
        self.assertEqual((folder_name, uid), ("Drafts", "draft-1"))

    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_read_attachment_data_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = ("file.txt", b"data")
        service = MailService(registry=mock.Mock())

        result = service.read_attachment_data("acct-1", "INBOX", "1", 0)

        run_on_mail_thread.assert_called_once_with(
            service._read_attachment_data_unlocked,
            "acct-1",
            "INBOX",
            "1",
            0,
        )
        self.assertEqual(result, ("file.txt", b"data"))

    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_toggle_message_seen_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = {"updates": []}
        service = MailService(registry=mock.Mock())

        service.toggle_message_seen("acct-1", "INBOX", "42")

        run_on_mail_thread.assert_called_once_with(
            service._toggle_message_seen_unlocked,
            "acct-1",
            "INBOX",
            "42",
        )

    @mock.patch("post.mail.eds.run_on_mail_thread")
    def test_move_messages_to_trash_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = {"moved_uids": ["1"]}
        service = MailService(registry=mock.Mock())

        service.move_messages_to_trash("acct-1", "INBOX", ["1"])

        run_on_mail_thread.assert_called_once_with(
            service._move_messages_to_trash_unlocked,
            "acct-1",
            "INBOX",
            ["1"],
        )


class FolderIndexInvalidationTests(unittest.TestCase):
    def test_invalidate_folder_index_keeps_correspondent_cache(self) -> None:
        service = MailService(registry=mock.Mock())
        cached = [mock.Mock()]
        service._correspondent_indexes["acct-1"] = cached

        service.invalidate_folder_index("acct-1", "Drafts")

        self.assertIs(service._correspondent_indexes.get("acct-1"), cached)

    def test_invalidate_correspondent_index_clears_cache(self) -> None:
        service = MailService(registry=mock.Mock())
        service._correspondent_indexes["acct-1"] = [mock.Mock()]

        service.invalidate_correspondent_index("acct-1")

        self.assertNotIn("acct-1", service._correspondent_indexes)


class InboxFolderNameTests(unittest.TestCase):
    def test_get_inbox_folder_name_uses_cached_tree(self) -> None:
        service = MailService(registry=mock.Mock())
        service._folder_tree_cache["acct-1"] = [
            {
                "full_name": "[Gmail]/Inbox",
                "display_name": "Inbox",
                "folder_type": 1,
            }
        ]

        with mock.patch.object(service, "list_folders") as list_folders:
            inbox = service.get_inbox_folder_name("acct-1")

        list_folders.assert_not_called()
        self.assertEqual(inbox, "[Gmail]/Inbox")


class ListFoldersOfflineBootstrapTests(unittest.TestCase):
    def test_offline_uses_local_bootstrap_when_no_memory_cache(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = False
        local_folders = [
            {
                "full_name": "INBOX",
                "display_name": "Inbox",
                "unread": -1,
                "total": -1,
                "flags": 1024,
            }
        ]

        with mock.patch.object(
            service,
            "_list_folders_from_local_store_unlocked",
            return_value=local_folders,
        ) as local_bootstrap:
            result = service._list_folders_unlocked("acct-1")

        local_bootstrap.assert_called_once_with("acct-1")
        self.assertEqual(result, local_folders)
        self.assertEqual(service._folder_tree_cache["acct-1"], local_folders)

    def test_offline_prefers_memory_cache_over_local_bootstrap(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = False
        cached = [{"full_name": "Sent", "display_name": "Sent"}]
        service._folder_tree_cache["acct-1"] = cached

        with mock.patch.object(
            service,
            "_list_folders_from_local_store_unlocked",
        ) as local_bootstrap:
            result = service._list_folders_unlocked("acct-1")

        local_bootstrap.assert_not_called()
        self.assertEqual(result, cached)

    def test_server_failure_falls_back_to_local_bootstrap(self) -> None:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        service = MailService(registry=mock.Mock())
        service._network_available = True
        store = mock.Mock()
        store.get_folder_info_sync.side_effect = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"),
            "Network is unreachable",
            39,
        )
        local_folders = [{"full_name": "INBOX", "display_name": "Inbox"}]

        with (
            mock.patch.object(service, "_get_store_unlocked", return_value=store),
            mock.patch.object(
                service,
                "_list_folders_from_local_store_unlocked",
                return_value=local_folders,
            ) as local_bootstrap,
        ):
            result = service._list_folders_unlocked("acct-1")

        local_bootstrap.assert_called_once_with("acct-1")
        self.assertEqual(result, local_folders)

    def test_null_folder_info_does_not_cache_empty(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        cached = [{"full_name": "Inbox", "display_name": "Inbox"}]
        service._folder_tree_cache["acct-1"] = list(cached)
        store = mock.Mock()
        store.get_folder_info_sync.return_value = None

        with mock.patch.object(service, "_get_store_unlocked", return_value=store):
            result = service._list_folders_unlocked("acct-1")

        self.assertEqual(result, cached)
        self.assertEqual(service._folder_tree_cache["acct-1"], cached)

    def test_null_folder_info_without_cache_raises(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        store = mock.Mock()
        store.get_folder_info_sync.return_value = None

        with (
            mock.patch.object(service, "_get_store_unlocked", return_value=store),
            mock.patch.object(
                service,
                "_list_folders_from_local_store_unlocked",
                return_value=[],
            ),
        ):
            with self.assertRaises(RuntimeError):
                service._list_folders_unlocked("acct-1")

        self.assertNotIn("acct-1", service._folder_tree_cache)

    def test_cancelled_folder_list_raises(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        service = MailService(registry=mock.Mock())
        cancellable = Gio.Cancellable()
        cancellable.cancel()

        with self.assertRaises(GLib.Error) as ctx:
            service._list_folders_unlocked("acct-1", cancellable=cancellable)
        self.assertTrue(
            ctx.exception.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
        )


class OfflineTransferQueueTests(unittest.TestCase):
    def test_transfer_queues_when_offline(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = False
        folder = mock.Mock()
        folder.get_full_name.return_value = "Trash"
        source_folder = mock.Mock()
        service._open_folder_unlocked = mock.Mock(return_value=source_folder)
        service._transfer_uids_in_folder = mock.Mock(return_value=["1"])

        with mock.patch.object(
            service,
            "_queue_transfer_operation_unlocked",
            return_value={"moved_uids": ["1"], "queued": True},
        ) as queue_transfer:
            result = service._transfer_messages_unlocked(
                "acct-1",
                "INBOX",
                ["1"],
                folder,
                op_type="move_to_trash",
            )

        queue_transfer.assert_called_once()
        self.assertTrue(result.get("queued"))
