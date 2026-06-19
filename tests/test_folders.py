# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.eds import MailAccount
from post.mail.folders import (
    find_inbox_folder,
    format_folder_label,
    guess_inbox_name,
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


class MailAccountDisplayLabelTests(unittest.TestCase):
    def test_prefers_email(self) -> None:
        account = MailAccount(
            uid="x", name="Work", email="user@example.com", backend="imapx"
        )
        self.assertEqual(account.display_label, "user@example.com")

    def test_falls_back_to_name(self) -> None:
        account = MailAccount(uid="x", name="Work", email=None, backend="imapx")
        self.assertEqual(account.display_label, "Work")


if __name__ == "__main__":
    unittest.main()
