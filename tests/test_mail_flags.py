# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail import message_flags


class ApplyMessageFlagsTests(unittest.TestCase):
    def test_uses_folder_set_message_flags_and_marks_folder_flagged(self) -> None:
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        folder.set_message_flags.return_value = True

        changed = message_flags.apply_message_flags(
            folder,
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
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = Camel.MessageFlags.SEEN
        folder.get_message_info.return_value = info

        changed = message_flags.apply_message_flags(
            folder,
            "42",
            Camel.MessageFlags.SEEN,
            Camel.MessageFlags.SEEN,
        )

        self.assertFalse(changed)
        folder.set_message_flags.assert_not_called()
        info.set_folder_flagged.assert_not_called()


class MarkMessageSeenTests(unittest.TestCase):
    def test_mark_seen_persists_when_changed(self) -> None:
        folder = MagicMock()
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1
        persist = MagicMock()

        with patch.object(
            message_flags,
            "apply_message_flags",
            return_value=True,
        ) as apply_mock:
            unread, total = message_flags.mark_message_seen(
                folder,
                "42",
                persist_uids=persist,
            )

        apply_mock.assert_called_once()
        persist.assert_called_once_with(["42"])
        self.assertEqual((unread, total), (0, 1))

    def test_mark_seen_skips_persist_when_unchanged(self) -> None:
        folder = MagicMock()
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1
        persist = MagicMock()

        with patch.object(
            message_flags,
            "apply_message_flags",
            return_value=False,
        ):
            message_flags.mark_message_seen(
                folder,
                "42",
                persist_uids=persist,
            )

        persist.assert_not_called()


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

        message_flags.persist_folder_flags(store, folder, ["42"])

        summary.touch.assert_called_once_with()
        summary.save.assert_called_once_with()
        folder.synchronize_message_sync.assert_called_once_with("42", None)
        folder.synchronize_sync.assert_called_once_with(False, None)
        store.synchronize_sync.assert_called_once_with(False, None)


class FollowUpFlagTests(unittest.TestCase):
    def test_uses_follow_up_flag_for_exchange_backends(self) -> None:
        self.assertTrue(message_flags.uses_follow_up_flag("microsoft365"))
        self.assertTrue(message_flags.uses_follow_up_flag("EWS"))
        self.assertFalse(message_flags.uses_follow_up_flag("imapx"))
        self.assertFalse(message_flags.uses_follow_up_flag(None))

    def test_message_info_is_flagged_imap_uses_flagged_bit(self) -> None:
        info = MagicMock()
        info.get_flags.return_value = Camel.MessageFlags.FLAGGED
        self.assertTrue(message_flags.message_info_is_flagged(info, backend="imapx"))
        info.get_flags.return_value = 0
        self.assertFalse(message_flags.message_info_is_flagged(info, backend="imapx"))

    def test_message_info_is_flagged_m365_uses_follow_up_tags(self) -> None:
        info = MagicMock()
        info.get_flags.return_value = Camel.MessageFlags.FLAGGED
        info.get_user_tag.side_effect = lambda name: {
            "follow-up": "follow-up",
            "completed-on": None,
        }.get(name)
        self.assertTrue(
            message_flags.message_info_is_flagged(info, backend="microsoft365")
        )

        info.get_user_tag.side_effect = lambda name: {
            "follow-up": "follow-up",
            "completed-on": "Wed, 06 Aug 2026 12:00:00 +0000",
        }.get(name)
        self.assertFalse(
            message_flags.message_info_is_flagged(info, backend="microsoft365")
        )

        info.get_user_tag.side_effect = lambda name: None
        self.assertFalse(
            message_flags.message_info_is_flagged(info, backend="microsoft365")
        )

    def test_apply_message_flagged_imap_uses_flagged_bit(self) -> None:
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        folder.set_message_flags.return_value = True
        on_changed = MagicMock()

        changed = message_flags.apply_message_flagged(
            folder,
            "42",
            True,
            backend="imapx",
            on_flagged_changed=on_changed,
        )

        self.assertTrue(changed)
        folder.set_message_flags.assert_called_once_with(
            "42",
            Camel.MessageFlags.FLAGGED,
            Camel.MessageFlags.FLAGGED,
        )
        on_changed.assert_called_once_with(True)

    def test_apply_message_flagged_m365_sets_follow_up_tags(self) -> None:
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        info.get_user_tag.return_value = None
        info.set_user_tag.return_value = True
        folder.get_message_info.return_value = info
        on_changed = MagicMock()

        changed = message_flags.apply_message_flagged(
            folder,
            "42",
            True,
            backend="microsoft365",
            on_flagged_changed=on_changed,
        )

        self.assertTrue(changed)
        folder.set_message_flags.assert_not_called()
        info.set_user_tag.assert_any_call("follow-up", "follow-up")
        info.set_folder_flagged.assert_called_once_with(True)
        on_changed.assert_called_once_with(True)

    def test_apply_message_flagged_m365_clears_follow_up_tags(self) -> None:
        folder = MagicMock()
        tags = {
            "follow-up": "follow-up",
            "completed-on": None,
            "due-by": "Wed, 06 Aug 2026 12:00:00 +0000",
            "follow-up-start": "Wed, 06 Aug 2026 10:00:00 +0000",
        }
        info = MagicMock()
        info.get_flags.return_value = 0
        info.get_user_tag.side_effect = lambda name: tags.get(name)
        info.set_user_tag.side_effect = lambda name, value: tags.__setitem__(
            name, value
        ) or True
        folder.get_message_info.return_value = info

        changed = message_flags.apply_message_flagged(
            folder,
            "42",
            False,
            backend="ews",
        )

        self.assertTrue(changed)
        folder.set_message_flags.assert_not_called()
        cleared = {
            call.args[0]
            for call in info.set_user_tag.call_args_list
            if call.args[1] is None
        }
        self.assertEqual(
            cleared,
            {"follow-up", "completed-on", "due-by", "follow-up-start"},
        )


@unittest.skipUnless(
    os.environ.get("POST_EDS_TESTS"),
    "Set POST_EDS_TESTS=1 to run EDS integration tests",
)
class ReadMessageTests(unittest.TestCase):
    @patch("post.mail.eds.MailService._mark_message_seen_unlocked")
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_read_message_can_skip_mark_seen(
        self,
        get_store_mock: MagicMock,
        mark_seen_mock: MagicMock,
    ) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=MagicMock())
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        mime = MagicMock()
        mime.get_message_id.return_value = None
        folder.get_message_sync.return_value = mime
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store

        with patch("post.mail.helpers.message_info_to_dict", return_value={"uid": "42"}):
            with patch(
                "post.mail.helpers.extract_message_bodies",
                return_value={"plain": "Hello", "html": None},
            ):
                with patch("post.mail.helpers.extract_attachments", return_value=[]):
                    result = service._read_message_unlocked(
                        "account",
                        "INBOX",
                        "42",
                        mark_seen=False,
                    )

        mark_seen_mock.assert_not_called()
        self.assertFalse((result.get("flags") or {}).get("seen"))


if __name__ == "__main__":
    unittest.main()
