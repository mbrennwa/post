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
