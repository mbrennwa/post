# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.eds import MailService


class ApplyMessageFlagsTests(unittest.TestCase):
    def test_uses_folder_set_message_flags_and_marks_folder_flagged(self) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        folder.set_message_flags.return_value = True

        changed = service._apply_message_flags_unlocked(
            folder,
            "account",
            "INBOX",
            "42",
            Camel.MessageFlags.SEEN,
            Camel.MessageFlags.SEEN,
        )

        self.assertTrue(changed)
        folder.set_message_flags.assert_called_once_with(
            "42",
            Camel.MessageFlags.SEEN,
            Camel.MessageFlags.SEEN,
        )
        info.set_folder_flagged.assert_called_once_with(True)

    def test_skips_when_flags_already_match(self) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = Camel.MessageFlags.SEEN
        folder.get_message_info.return_value = info

        changed = service._apply_message_flags_unlocked(
            folder,
            "account",
            "INBOX",
            "42",
            Camel.MessageFlags.SEEN,
            Camel.MessageFlags.SEEN,
        )

        self.assertFalse(changed)
        folder.set_message_flags.assert_not_called()
        info.set_folder_flagged.assert_not_called()

    @patch.object(MailService, "_persist_message_flag_changes_unlocked")
    def test_mark_seen_persists_when_changed(
        self, persist_mock: MagicMock
    ) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1

        with patch.object(
            service,
            "_apply_message_flags_unlocked",
            return_value=True,
        ) as apply_mock:
            unread, total = service._mark_message_seen_unlocked(
                folder, "account", "INBOX", "42"
            )

        apply_mock.assert_called_once()
        persist_mock.assert_called_once_with("account", folder, ["42"])
        self.assertEqual((unread, total), (0, 1))

    @patch.object(MailService, "_persist_message_flag_changes_unlocked")
    def test_mark_seen_skips_persist_when_unchanged(
        self, persist_mock: MagicMock
    ) -> None:
        service = MailService(registry=MagicMock())
        folder = MagicMock()
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1

        with patch.object(
            service,
            "_apply_message_flags_unlocked",
            return_value=False,
        ):
            service._mark_message_seen_unlocked(folder, "account", "INBOX", "42")

        persist_mock.assert_not_called()


class PersistFolderFlagsTests(unittest.TestCase):
    def test_saves_summary_and_synchronizes(self) -> None:
        store = MagicMock()
        store.get_connection_status.return_value = (
            Camel.ServiceConnectionStatus.CONNECTED
        )
        store.synchronize_sync.return_value = True
        folder = MagicMock()
        folder.get_parent_store.return_value = store
        summary = MagicMock()
        summary.save.return_value = True
        folder.get_folder_summary.return_value = summary
        folder.synchronize_message_sync.return_value = True
        folder.synchronize_sync.return_value = True

        MailService._persist_folder_flags_unlocked(store, folder, ["42"])

        summary.touch.assert_called_once_with()
        summary.save.assert_called_once_with()
        folder.synchronize_message_sync.assert_called_once_with("42", None)
        folder.synchronize_sync.assert_called_once_with(False, None)
        store.synchronize_sync.assert_called_once_with(False, None)


if __name__ == "__main__":
    unittest.main()
