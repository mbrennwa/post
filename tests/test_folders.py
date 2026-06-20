# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.eds import MailAccount
from post.mail.folders import (
    find_folder_by_type,
    find_inbox_folder,
    format_folder_label,
    guess_inbox_name,
    resolve_move_menu_state,
)


class FormatFolderLabelTests(unittest.TestCase):
    def test_unread_and_total(self) -> None:
        self.assertEqual(format_folder_label("Inbox", 3, 42), "Inbox (3/42)")

    def test_total_only(self) -> None:
        self.assertEqual(format_folder_label("Sent", -1, 10), "Sent (10)")

    def test_unread_only(self) -> None:
        self.assertEqual(format_folder_label("Drafts", 2, -1), "Drafts (2)")

    def test_no_counts(self) -> None:
        self.assertEqual(format_folder_label("Archive", -1, -1), "Archive")


class GuessInboxTests(unittest.TestCase):
    def test_inbox_full_name(self) -> None:
        folders = [
            {"full_name": "Sent", "display_name": "Sent"},
            {"full_name": "INBOX", "display_name": "Inbox"},
        ]
        self.assertEqual(guess_inbox_name(folders), "INBOX")

    def test_inbox_display_name(self) -> None:
        folders = [
            {"full_name": "mail/inbox", "display_name": "Inbox"},
            {"full_name": "mail/sent", "display_name": "Sent"},
        ]
        self.assertEqual(guess_inbox_name(folders), "mail/inbox")

    def test_fallback_first_folder(self) -> None:
        folders = [{"full_name": "Archive", "display_name": "Archive"}]
        self.assertEqual(guess_inbox_name(folders), "Archive")

    def test_empty(self) -> None:
        self.assertIsNone(guess_inbox_name([]))


class FindInboxFolderTests(unittest.TestCase):
    def test_returns_matching_folder_dict(self) -> None:
        inbox = {"full_name": "INBOX", "display_name": "Inbox", "unread": 1}
        folders = [{"full_name": "Sent", "display_name": "Sent"}, inbox]
        self.assertEqual(find_inbox_folder(folders), inbox)


class FindFolderByTypeTests(unittest.TestCase):
    TYPE_ARCHIVE = 11264
    TYPE_TRASH = 10240
    TYPE_MASK = 64512

    def test_matches_folder_type_flag(self) -> None:
        archive = {
            "full_name": "Archive",
            "display_name": "Archive",
            "flags": self.TYPE_ARCHIVE,
        }
        folders = [{"full_name": "INBOX", "display_name": "Inbox", "flags": 1024}, archive]
        self.assertEqual(
            find_folder_by_type(
                folders, self.TYPE_ARCHIVE, type_mask=self.TYPE_MASK
            ),
            archive,
        )

    def test_falls_back_to_display_name(self) -> None:
        archive = {"full_name": "mail/archive", "display_name": "Archive", "flags": 0}
        self.assertEqual(
            find_folder_by_type(
                folders=[{"full_name": "INBOX", "display_name": "Inbox"}, archive],
                folder_type=99999,
                type_mask=self.TYPE_MASK,
                name_fallbacks=frozenset({"archive"}),
            ),
            archive,
        )


class ResolveMoveMenuStateTests(unittest.TestCase):
    TYPE_ARCHIVE = 11264
    TYPE_TRASH = 10240
    TYPE_MASK = 64512

    def test_allows_archive_and_trash_from_inbox(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "Archive", "display_name": "Archive", "flags": 11264},
            {"full_name": "Trash", "display_name": "Trash", "flags": 10240},
        ]
        state = resolve_move_menu_state(
            folders,
            "INBOX",
            archive_type=self.TYPE_ARCHIVE,
            trash_type=self.TYPE_TRASH,
            type_mask=self.TYPE_MASK,
        )
        self.assertTrue(state["can_archive"])
        self.assertTrue(state["can_trash"])

    def test_disables_archive_when_already_there(self) -> None:
        folders = [
            {"full_name": "Archive", "display_name": "Archive", "flags": 11264},
            {"full_name": "Trash", "display_name": "Trash", "flags": 10240},
        ]
        state = resolve_move_menu_state(
            folders,
            "Archive",
            archive_type=self.TYPE_ARCHIVE,
            trash_type=self.TYPE_TRASH,
            type_mask=self.TYPE_MASK,
        )
        self.assertFalse(state["can_archive"])
        self.assertTrue(state["can_trash"])

    def test_disables_archive_when_folder_missing(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "Trash", "display_name": "Trash", "flags": 10240},
        ]
        state = resolve_move_menu_state(
            folders,
            "INBOX",
            archive_type=self.TYPE_ARCHIVE,
            trash_type=self.TYPE_TRASH,
            type_mask=self.TYPE_MASK,
        )
        self.assertFalse(state["can_archive"])
        self.assertTrue(state["can_trash"])


class MailAccountDisplayLabelTests(unittest.TestCase):
    def test_prefers_email(self) -> None:
        account = MailAccount(
            uid="x", name="Work", email="user@example.com", backend="imapx"
        )
        self.assertEqual(account.display_label, "user@example.com")

    def test_falls_back_to_name(self) -> None:
        account = MailAccount(uid="x", name="Work", email=None, backend="imapx")
        self.assertEqual(account.display_label, "Work")

    def test_can_send_requires_transport_and_from(self) -> None:
        ready = MailAccount(
            uid="x",
            name="Work",
            email="user@example.com",
            backend="imapx",
            from_address="user@example.com",
            transport_uid="transport-1",
        )
        missing_transport = MailAccount(
            uid="y",
            name="Work",
            email="user@example.com",
            backend="imapx",
            from_address="user@example.com",
        )
        self.assertTrue(ready.can_send)
        self.assertFalse(missing_transport.can_send)


if __name__ == "__main__":
    unittest.main()
