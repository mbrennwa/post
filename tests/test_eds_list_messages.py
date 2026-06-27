# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService
from post.mail.search import MessageSearchQuery, SearchTerm


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
    def test_search_messages_page_uses_mail_thread(self, run_on_mail_thread) -> None:
        run_on_mail_thread.return_value = ([], 0, 0, False)
        service = MailService(registry=mock.Mock())
        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="hello"),))

        service.search_messages_page(
            "acct-1",
            "INBOX",
            query,
            offset=5,
            limit=20,
            sync=False,
        )

        run_on_mail_thread.assert_called_once_with(
            service._search_messages_page_unlocked,
            "acct-1",
            "INBOX",
            query,
            offset=5,
            limit=20,
            sync=False,
        )
