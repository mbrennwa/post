# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class MutationDispatchTests(unittest.TestCase):
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
