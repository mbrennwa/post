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
