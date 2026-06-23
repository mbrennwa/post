# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel, GLib

from post.mail.eds import MailService, MessageNotAvailableError


class MessageNotAvailableErrorTests(unittest.TestCase):
    def test_user_message(self) -> None:
        exc = MessageNotAvailableError("53054", "INBOX")
        self.assertEqual(exc.message_uid, "53054")
        self.assertEqual(exc.folder_name, "INBOX")
        self.assertEqual(exc.user_message(), "This message is no longer available.")


class MissingMessageErrorDetectionTests(unittest.TestCase):
    def test_is_missing_message_error_matches_invalid_uid(self) -> None:
        service = MailService(registry=MagicMock())
        exc = GLib.Error.new_literal(
            Camel.folder_error_quark(),
            "Cannot get message with message ID 53054: No such message available.",
            int(Camel.FolderError.INVALID_UID),
        )
        self.assertTrue(service._is_missing_message_error(exc))

    def test_is_missing_message_error_rejects_other_errors(self) -> None:
        service = MailService(registry=MagicMock())
        exc = GLib.Error.new_literal(
            Camel.folder_error_quark(),
            "Folder is invalid",
            int(Camel.FolderError.INVALID),
        )
        self.assertFalse(service._is_missing_message_error(exc))


class ReadMessageUnavailableTests(unittest.TestCase):
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_read_message_raises_when_camel_reports_invalid_uid(
        self,
        get_store_mock: MagicMock,
    ) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        folder.get_message_info.return_value = MagicMock()
        folder.get_message_sync.side_effect = GLib.Error.new_literal(
            Camel.folder_error_quark(),
            "Cannot get message with message ID 53054: No such message available.",
            int(Camel.FolderError.INVALID_UID),
        )
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._read_message_unlocked("account", "INBOX", "53054")

        self.assertEqual(ctx.exception.message_uid, "53054")
        self.assertEqual(ctx.exception.folder_name, "INBOX")

    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_read_attachment_raises_when_message_missing(
        self,
        get_store_mock: MagicMock,
    ) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        folder.get_message_sync.return_value = None
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._read_attachment_data_unlocked(
                "account", "INBOX", "42", 0
            )

        self.assertEqual(ctx.exception.message_uid, "42")


if __name__ == "__main__":
    unittest.main()
