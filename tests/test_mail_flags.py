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


class ReadMessageSignInTests(unittest.TestCase):
    @patch("post.mail.eds.MailService._mark_message_seen_unlocked")
    @patch("post.mail.eds.MailService._get_message_mime_sync")
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_mark_seen_auth_failure_still_returns_body(
        self,
        get_store_mock: MagicMock,
        get_mime_mock: MagicMock,
        mark_seen_mock: MagicMock,
    ) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=MagicMock())
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store
        get_mime_mock.return_value = mime = MagicMock()
        mime.get_message_id.return_value = None
        mime.get_header.return_value = None
        mark_seen_mock.side_effect = RuntimeError(
            "Failed to refresh access token (goa-error-quark, 4): AADSTS70043"
        )

        with patch("post.mail.helpers.message_info_to_dict", return_value={"uid": "42"}):
            with patch(
                "post.mail.helpers.extract_message_bodies",
                return_value={"plain": "Hello", "html": None},
            ):
                with patch("post.mail.helpers.extract_attachments", return_value=[]):
                    with patch(
                        "post.mail.helpers.extract_inline_images", return_value=[]
                    ):
                        result = service._read_message_unlocked(
                            "acct-1",
                            "INBOX",
                            "42",
                        )

        self.assertEqual(result["body_plain"], "Hello")
        self.assertTrue((result.get("flags") or {}).get("seen"))
        self.assertEqual(service.get_account_connect_health("acct-1"), "needs_sign_in")


class ReadMessageGoaUnavailableTests(unittest.TestCase):
    def _goa_account(self, service: object) -> None:
        source = MagicMock()
        source.has_extension.return_value = True
        service.registry.ref_source.return_value = source

    def _read_with_mocked_mime(
        self,
        service,
        *,
        get_mime_mock: MagicMock,
        get_store_mock: MagicMock,
        mark_seen: bool = True,
    ) -> dict:
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        folder.get_unread_message_count.return_value = 0
        folder.get_message_count.return_value = 1
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store
        mime = MagicMock()
        mime.get_message_id.return_value = None
        mime.get_header.return_value = None
        get_mime_mock.return_value = mime
        with patch("post.mail.helpers.message_info_to_dict", return_value={"uid": "42"}):
            with patch(
                "post.mail.helpers.extract_message_bodies",
                return_value={"plain": "Hello", "html": None},
            ):
                with patch("post.mail.helpers.extract_attachments", return_value=[]):
                    with patch(
                        "post.mail.helpers.extract_inline_images", return_value=[]
                    ):
                        return service._read_message_unlocked(
                            "acct-1",
                            "INBOX",
                            "42",
                            mark_seen=mark_seen,
                        )

    @patch("post.mail.eds.ensure_goa_credentials", return_value=False)
    @patch("post.mail.eds.MailService._mark_message_seen_unlocked")
    @patch("post.mail.eds.MailService._get_message_mime_sync")
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_goa_unavailable_cache_hit_returns_body(
        self,
        get_store_mock: MagicMock,
        get_mime_mock: MagicMock,
        mark_seen_mock: MagicMock,
        ensure_goa_mock: MagicMock,
    ) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=MagicMock())
        self._goa_account(service)
        mark_seen_mock.return_value = (0, 1)

        result = self._read_with_mocked_mime(
            service,
            get_mime_mock=get_mime_mock,
            get_store_mock=get_store_mock,
        )

        ensure_goa_mock.assert_called_once()
        get_store_mock.assert_called_once()
        self.assertFalse(get_store_mock.call_args.kwargs.get("allow_online", True))
        self.assertFalse(get_mime_mock.call_args.kwargs.get("allow_network", True))
        self.assertEqual(result["body_plain"], "Hello")
        self.assertTrue((result.get("flags") or {}).get("seen"))
        mark_seen_mock.assert_called_once()
        self.assertEqual(service.get_account_connect_health("acct-1"), "needs_sign_in")

    @patch("post.mail.eds.ensure_goa_credentials", return_value=False)
    @patch("post.mail.eds.MailService._mark_message_seen_unlocked")
    @patch("post.mail.eds.MailService._get_message_mime_sync")
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_goa_unavailable_cache_miss_leaves_unread(
        self,
        get_store_mock: MagicMock,
        get_mime_mock: MagicMock,
        mark_seen_mock: MagicMock,
        ensure_goa_mock: MagicMock,
    ) -> None:
        from post.mail.eds import (
            MailService,
            MessageNotAvailableError,
            MessageUnavailableReason,
        )

        service = MailService(registry=MagicMock())
        self._goa_account(service)
        folder = MagicMock()
        info = MagicMock()
        info.get_flags.return_value = 0
        folder.get_message_info.return_value = info
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store
        get_mime_mock.side_effect = MessageNotAvailableError(
            "42",
            "INBOX",
            reason=MessageUnavailableReason.NOT_CACHED_SIGN_IN,
        )

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._read_message_unlocked("acct-1", "INBOX", "42")

        self.assertEqual(
            ctx.exception.reason, MessageUnavailableReason.NOT_CACHED_SIGN_IN
        )
        mark_seen_mock.assert_not_called()
        get_store_mock.assert_called_once()
        self.assertFalse(get_mime_mock.call_args.kwargs.get("allow_network", True))
        self.assertEqual(service.get_account_connect_health("acct-1"), "needs_sign_in")


class PersistFlagSignInTests(unittest.TestCase):
    @patch("post.mail.eds.MailService._queue_flag_operation_unlocked")
    @patch("post.mail.eds.MailService._get_store_unlocked")
    @patch("post.mail.eds.MailService._persist_folder_flags_unlocked")
    def test_sign_in_error_queues_seen(
        self,
        persist_mock: MagicMock,
        get_store_mock: MagicMock,
        queue_mock: MagicMock,
    ) -> None:
        from post.mail.eds import MailService

        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        folder.get_full_name.return_value = "INBOX"
        persist_mock.side_effect = RuntimeError(
            "Failed to refresh access token (goa-error-quark, 4): AADSTS70043"
        )

        queued = service._persist_message_flag_changes_unlocked(
            "acct-1",
            folder,
            ["42"],
            op_type="set_seen",
            seen=True,
        )

        self.assertTrue(queued)
        queue_mock.assert_called_once()
        self.assertEqual(service.get_account_connect_health("acct-1"), "needs_sign_in")


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
