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
