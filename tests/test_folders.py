# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.accounts import MailAccount
from post.mail.folders import (
    POST_OUTBOX_FOLDER,
    filter_sidebar_folders,
    find_folder_by_type,
    find_inbox_folder,
    find_trash_folder,
    folder_can_contain_messages,
    folder_name_from_uri,
    format_folder_label,
    guess_inbox_name,
    is_post_outbox_folder,
    is_drafts_folder,
    is_drafts_folder_name,
    is_system_folder,
    outbox_folder_dict,
    resolve_move_menu_state,
    resolve_sidebar_context_menu,
    validate_folder_display_name,
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


class FolderCanContainMessagesTests(unittest.TestCase):
    def test_noselect_folder_is_skipped(self) -> None:
        folder = {"full_name": "[GoogleMail]", "flags": 1}
        self.assertFalse(folder_can_contain_messages(folder))

    def test_virtual_folder_is_skipped(self) -> None:
        folder = {"full_name": ".#evolution/trash", "flags": 0}
        self.assertFalse(folder_can_contain_messages(folder))

    def test_regular_inbox_is_included(self) -> None:
        folder = {"full_name": "INBOX", "flags": 1024}
        self.assertTrue(folder_can_contain_messages(folder))


class FolderNameFromUriTests(unittest.TestCase):
    def test_local_sent_folder(self) -> None:
        self.assertEqual(folder_name_from_uri("folder://local/Sent"), "Sent")

    def test_gmail_sent_folder(self) -> None:
        self.assertEqual(
            folder_name_from_uri("folder://local/[Gmail]/Sent Mail"),
            "[Gmail]/Sent Mail",
        )

    def test_empty(self) -> None:
        self.assertIsNone(folder_name_from_uri(None))
        self.assertIsNone(folder_name_from_uri(""))


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


class FindTrashFolderTests(unittest.TestCase):
    TYPE_TRASH = 3072
    TYPE_MASK = 64512

    def test_prefers_real_trash_over_virtual(self) -> None:
        folders = [
            {"full_name": "Trash", "display_name": "Trash", "flags": 24},
            {"full_name": ".#evolution/Trash", "display_name": "Trash", "flags": 3314},
        ]
        trash = find_trash_folder(
            folders,
            trash_type=self.TYPE_TRASH,
            type_mask=self.TYPE_MASK,
        )
        self.assertEqual(trash, folders[0])

    def test_resolve_move_menu_uses_real_trash(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "Trash", "display_name": "Trash", "flags": 24},
            {"full_name": ".#evolution/Trash", "display_name": "Trash", "flags": 3314},
        ]
        state = resolve_move_menu_state(
            folders,
            "INBOX",
            archive_type=11264,
            trash_type=self.TYPE_TRASH,
            type_mask=self.TYPE_MASK,
        )
        self.assertEqual(state["trash_folder"], "Trash")


class ResolveSidebarContextMenuTests(unittest.TestCase):
    def _folders(self) -> list[dict]:
        return [
            {
                "full_name": "INBOX",
                "display_name": "Inbox",
                "flags": 1024,
                "unread": 2,
                "total": 5,
            },
            {
                "full_name": "Archive",
                "display_name": "Archive",
                "flags": 11264,
                "unread": 0,
                "total": 0,
            },
            {
                "full_name": "Trash",
                "display_name": "Trash",
                "flags": 10240,
                "unread": 0,
                "total": 3,
            },
        ]

    def test_inbox_archive_read_visible_only_with_archive(self) -> None:
        folders = self._folders()
        with_archive = resolve_sidebar_context_menu(
            folders=folders,
            folder_name="INBOX",
            inbox_name="INBOX",
            trash_name="Trash",
            archive_name="Archive",
            unread=2,
            total=5,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertTrue(with_archive["show_archive_read"])
        self.assertTrue(with_archive["enable_archive_read"])
        self.assertTrue(with_archive["show_archive_read_unflagged"])
        self.assertTrue(with_archive["enable_archive_read_unflagged"])

        without_archive = resolve_sidebar_context_menu(
            folders=folders,
            folder_name="INBOX",
            inbox_name="INBOX",
            trash_name="Trash",
            archive_name=None,
            unread=2,
            total=5,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertFalse(without_archive["show_archive_read"])
        self.assertFalse(without_archive["show_archive_read_unflagged"])

    def test_inbox_archive_read_disabled_without_read_messages(self) -> None:
        state = resolve_sidebar_context_menu(
            folders=self._folders(),
            folder_name="INBOX",
            inbox_name="INBOX",
            trash_name="Trash",
            archive_name="Archive",
            unread=5,
            total=5,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertTrue(state["show_archive_read"])
        self.assertFalse(state["enable_archive_read"])
        self.assertTrue(state["show_archive_read_unflagged"])
        self.assertFalse(state["enable_archive_read_unflagged"])

    def test_outbox_send_now_enabled_with_queue(self) -> None:
        state = resolve_sidebar_context_menu(
            folders=[],
            folder_name=POST_OUTBOX_FOLDER,
            inbox_name="INBOX",
            trash_name="Trash",
            archive_name="Archive",
            unread=0,
            total=2,
            outbox_count=2,
            folder_crud_enabled=True,
        )
        self.assertTrue(state["show_send_now"])
        self.assertTrue(state["enable_send_now"])
        self.assertFalse(state["show_new_subfolder"])

    def test_outbox_send_now_disabled_when_empty(self) -> None:
        state = resolve_sidebar_context_menu(
            folders=[],
            folder_name=POST_OUTBOX_FOLDER,
            inbox_name="INBOX",
            trash_name=None,
            archive_name=None,
            unread=0,
            total=0,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertTrue(state["show_send_now"])
        self.assertFalse(state["enable_send_now"])

    def test_trash_empty_enabled_with_messages(self) -> None:
        state = resolve_sidebar_context_menu(
            folders=self._folders(),
            folder_name="Trash",
            inbox_name="INBOX",
            trash_name="Trash",
            archive_name="Archive",
            unread=0,
            total=3,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertTrue(state["show_empty_trash"])
        self.assertTrue(state["enable_empty_trash"])

    def test_account_new_folder_hidden_for_spool(self) -> None:
        state = resolve_sidebar_context_menu(
            folders=[],
            folder_name=None,
            inbox_name=None,
            trash_name=None,
            archive_name=None,
            unread=-1,
            total=-1,
            outbox_count=0,
            folder_crud_enabled=False,
        )
        self.assertFalse(state["show_new_folder"])


class IsSystemFolderTests(unittest.TestCase):
    TYPE_ARCHIVE = 11264
    TYPE_MASK = 64512

    def test_protects_camel_typed_archive(self) -> None:
        folder = {"full_name": "Archive", "display_name": "Archive", "flags": 11264}
        self.assertTrue(
            is_system_folder(folder, type_mask=self.TYPE_MASK)
        )

    def test_allows_user_folder_named_archives_without_type(self) -> None:
        folder = {"full_name": "Archives", "display_name": "Archives", "flags": 0}
        self.assertFalse(
            is_system_folder(folder, type_mask=self.TYPE_MASK)
        )

    def test_sidebar_allows_delete_for_user_archives_folder(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "mail/archive", "display_name": "Archive", "flags": 11264},
            {"full_name": "Archives", "display_name": "Archives", "flags": 0},
        ]
        state = resolve_sidebar_context_menu(
            folders=folders,
            folder_name="Archives",
            inbox_name="INBOX",
            trash_name=None,
            archive_name="mail/archive",
            unread=0,
            total=1,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertTrue(state["show_delete"])
        self.assertTrue(state["show_rename"])

    def test_sidebar_blocks_delete_for_resolved_archive_path(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "mail/archive", "display_name": "Archive", "flags": 11264},
        ]
        state = resolve_sidebar_context_menu(
            folders=folders,
            folder_name="mail/archive",
            inbox_name="INBOX",
            trash_name=None,
            archive_name="mail/archive",
            unread=0,
            total=0,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertFalse(state["show_delete"])

    def test_sent_folder_without_type_flags_is_protected(self) -> None:
        folder = {"full_name": "Sent", "display_name": "Sent", "flags": 0}
        self.assertTrue(is_system_folder(folder, type_mask=self.TYPE_MASK))

    def test_sidebar_blocks_delete_for_sent_folder(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "Sent", "display_name": "Sent", "flags": 0},
        ]
        state = resolve_sidebar_context_menu(
            folders=folders,
            folder_name="Sent",
            inbox_name="INBOX",
            trash_name=None,
            archive_name=None,
            unread=0,
            total=5,
            outbox_count=0,
            folder_crud_enabled=True,
        )
        self.assertFalse(state["show_delete"])
        self.assertFalse(state["show_rename"])


class ValidateFolderDisplayNameTests(unittest.TestCase):
    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_folder_display_name("   ")

    def test_rejects_slashes(self) -> None:
        with self.assertRaises(ValueError):
            validate_folder_display_name("a/b")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(validate_folder_display_name("  Work  "), "Work")


class FilterSidebarFoldersTests(unittest.TestCase):
    def test_hides_virtual_trash_when_real_trash_exists(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox"},
            {"full_name": "Trash", "display_name": "Trash"},
            {"full_name": ".#evolution/Trash", "display_name": "Trash"},
        ]
        filtered = filter_sidebar_folders(folders)
        names = [folder["full_name"] for folder in filtered]
        self.assertIn("Trash", names)
        self.assertNotIn(".#evolution/Trash", names)


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


class DraftsFolderTests(unittest.TestCase):
    def test_is_drafts_folder_by_type(self) -> None:
        folder = {"full_name": "Drafts", "display_name": "Drafts", "flags": 12288}
        self.assertTrue(is_drafts_folder(folder))

    def test_is_drafts_folder_by_name(self) -> None:
        folder = {"full_name": "mail/drafts", "display_name": "Drafts", "flags": 0}
        self.assertTrue(is_drafts_folder(folder))

    def test_is_drafts_folder_name_lookup(self) -> None:
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
            {"full_name": "Drafts", "display_name": "Drafts", "flags": 12288},
        ]
        self.assertTrue(is_drafts_folder_name(folders, "Drafts"))
        self.assertFalse(is_drafts_folder_name(folders, "INBOX"))


class PostOutboxFolderTests(unittest.TestCase):
    def test_sentinel_folder(self) -> None:
        self.assertEqual(POST_OUTBOX_FOLDER, ".post/Outbox")
        self.assertTrue(is_post_outbox_folder(POST_OUTBOX_FOLDER))
        self.assertFalse(is_post_outbox_folder("INBOX"))

    def test_outbox_folder_dict(self) -> None:
        folder = outbox_folder_dict(3)
        self.assertEqual(folder["full_name"], POST_OUTBOX_FOLDER)
        self.assertEqual(folder["display_name"], "Outbox")
        self.assertEqual(folder["total"], 3)


if __name__ == "__main__":
    unittest.main()
