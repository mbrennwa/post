# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel, GLib

from post.mail.eds import (
    MailService,
    MessageNotAvailableError,
    MessageUnavailableReason,
)


def _invalid_uid_error(message_uid: str = "53054") -> GLib.Error:
    return GLib.Error.new_literal(
        Camel.folder_error_quark(),
        f"Cannot get message with message ID {message_uid}: "
        "No such message available.",
        int(Camel.FolderError.INVALID_UID),
    )


def _graph_item_not_found_error(message_uid: str = "53054") -> GLib.Error:
    return GLib.Error.new_literal(
        Camel.folder_error_quark(),
        "ErrorItemNotFound: The specified object was not found in the store., "
        "The process failed to get the correct properties.",
        int(Camel.FolderError.INVALID_UID),
    )


class MessageNotAvailableErrorTests(unittest.TestCase):
    def test_user_message(self) -> None:
        exc = MessageNotAvailableError("53054", "INBOX")
        self.assertEqual(exc.message_uid, "53054")
        self.assertEqual(exc.folder_name, "INBOX")
        self.assertEqual(exc.user_message(), "This message is no longer available.")

    def test_not_cached_offline_message(self) -> None:
        exc = MessageNotAvailableError(
            "1",
            "INBOX",
            reason=MessageUnavailableReason.NOT_CACHED_OFFLINE,
        )
        self.assertIn("offline", exc.user_message().lower())


class MissingMessageErrorDetectionTests(unittest.TestCase):
    def test_is_missing_message_error_matches_invalid_uid(self) -> None:
        service = MailService(registry=MagicMock())
        self.assertTrue(service._is_missing_message_error(_invalid_uid_error()))

    def test_is_missing_message_error_rejects_other_errors(self) -> None:
        service = MailService(registry=MagicMock())
        exc = GLib.Error.new_literal(
            Camel.folder_error_quark(),
            "Folder is invalid",
            int(Camel.FolderError.INVALID),
        )
        self.assertFalse(service._is_missing_message_error(exc))


class ReadMessageUnavailableTests(unittest.TestCase):
    def test_online_invalid_uid_recovers_from_cache(self) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        cached = MagicMock(name="cached_mime")
        folder.get_message_sync.side_effect = _invalid_uid_error()
        folder.get_message_cached.return_value = cached

        mime = service._get_message_mime_sync(
            folder, "account", "Archive", "53054"
        )

        self.assertIs(mime, cached)
        folder.synchronize_message_sync.assert_not_called()

    def test_online_invalid_uid_synchronizes_and_retries(self) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        recovered = MagicMock(name="recovered_mime")
        folder.get_message_sync.side_effect = [
            _invalid_uid_error(),
            recovered,
        ]
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = MagicMock()
        folder.synchronize_message_sync.return_value = True

        mime = service._get_message_mime_sync(
            folder, "account", "Archive", "53054"
        )

        self.assertIs(mime, recovered)
        folder.synchronize_message_sync.assert_called_once_with("53054", None)

    def test_online_invalid_uid_confirmed_vanished(self) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        folder.get_message_sync.side_effect = _invalid_uid_error()
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = None

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._get_message_mime_sync(
                folder, "account", "INBOX", "53054"
            )

        self.assertEqual(ctx.exception.message_uid, "53054")
        self.assertEqual(ctx.exception.folder_name, "INBOX")
        self.assertEqual(ctx.exception.reason, MessageUnavailableReason.VANISHED)
        folder.synchronize_message_sync.assert_not_called()

    def test_online_invalid_uid_known_but_unfetchable_is_not_vanished(
        self,
    ) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        folder.get_message_sync.side_effect = _invalid_uid_error()
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = MagicMock()
        folder.synchronize_message_sync.return_value = True

        with self.assertRaises(RuntimeError) as ctx:
            service._get_message_mime_sync(
                folder, "account", "Archive", "53054"
            )

        self.assertIn("Could not load message", str(ctx.exception))
        self.assertFalse(isinstance(ctx.exception, MessageNotAvailableError))
        folder.synchronize_message_sync.assert_called_once_with("53054", None)

    def test_online_invalid_uid_recovers_when_only_folder_index_knows_uid(
        self,
    ) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = MailService(registry=MagicMock())
        service._network_available = True
        service._folder_indexes[("account", "Archive")] = _FolderMessageIndex(
            messages=[{"uid": "53054", "subject": "x"}],
            unread=0,
            total=1,
        )
        folder = MagicMock()
        recovered = MagicMock(name="recovered_mime")
        folder.get_message_sync.side_effect = [
            _invalid_uid_error(),
            recovered,
        ]
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = None
        folder.synchronize_message_sync.return_value = True

        mime = service._get_message_mime_sync(
            folder, "account", "Archive", "53054"
        )

        self.assertIs(mime, recovered)
        folder.synchronize_message_sync.assert_called_once_with("53054", None)

    def test_online_graph_item_not_found_index_only_keeps_row(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = MailService(registry=MagicMock())
        service._network_available = True
        service._folder_indexes[("account", "Archive")] = _FolderMessageIndex(
            messages=[{"uid": "stale-uid", "subject": "x", "message_id": "<a@x>"}],
            unread=0,
            total=1,
        )
        folder = MagicMock()
        folder.get_message_sync.side_effect = _graph_item_not_found_error(
            "stale-uid"
        )
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = None
        folder.synchronize_message_sync.side_effect = _graph_item_not_found_error(
            "stale-uid"
        )
        folder.dup_uids.return_value = []
        folder.get_uids.return_value = []

        with self.assertRaises(RuntimeError) as ctx:
            service._get_message_mime_sync(
                folder, "account", "Archive", "stale-uid"
            )

        self.assertIn("Could not load message", str(ctx.exception))
        self.assertFalse(isinstance(ctx.exception, MessageNotAvailableError))

    def test_online_recovers_via_alternate_folder_index_uid(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = MailService(registry=MagicMock())
        service._network_available = True
        service._folder_indexes[("account", "Archive")] = _FolderMessageIndex(
            messages=[
                {
                    "uid": "stale-uid",
                    "subject": "Hello",
                    "message_id": "<msg@example.com>",
                    "from": "a@b.c",
                    "sort_date": 1,
                },
                {
                    "uid": "live-uid",
                    "subject": "Hello",
                    "message_id": "<msg@example.com>",
                    "from": "a@b.c",
                    "sort_date": 1,
                },
            ],
            unread=0,
            total=2,
        )
        folder = MagicMock()
        live_mime = MagicMock(name="live_mime")

        def get_message_sync(uid: str, _cancel: object) -> object:
            if uid == "stale-uid":
                raise _graph_item_not_found_error("stale-uid")
            if uid == "live-uid":
                return live_mime
            raise _invalid_uid_error(uid)

        folder.get_message_sync.side_effect = get_message_sync
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = None
        folder.synchronize_message_sync.return_value = True
        folder.dup_uids.return_value = ["live-uid"]
        folder.get_uids.return_value = ["live-uid"]

        mime = service._get_message_mime_sync(
            folder, "account", "Archive", "stale-uid"
        )

        self.assertIs(mime, live_mime)
        self.assertFalse(
            any(
                str(m.get("uid")) == "stale-uid"
                for m in service._folder_indexes[("account", "Archive")].messages
            )
        )
    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_read_message_offline_not_cached(
        self,
        get_store_mock: MagicMock,
    ) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = False
        folder = MagicMock()
        folder.get_message_info.return_value = MagicMock()
        folder.get_message_sync.side_effect = _invalid_uid_error("1")
        folder.get_message_cached.return_value = None
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._read_message_unlocked("account", "INBOX", "1")

        self.assertEqual(
            ctx.exception.reason,
            MessageUnavailableReason.NOT_CACHED_OFFLINE,
        )

    @patch("post.mail.eds.MailService._get_store_unlocked")
    def test_read_attachment_raises_when_message_missing(
        self,
        get_store_mock: MagicMock,
    ) -> None:
        service = MailService(registry=MagicMock())
        service._network_available = True
        folder = MagicMock()
        folder.get_message_sync.return_value = None
        folder.get_message_cached.return_value = None
        folder.get_message_info.return_value = None
        store = MagicMock()
        store.get_folder_sync.return_value = folder
        get_store_mock.return_value = store

        with self.assertRaises(MessageNotAvailableError) as ctx:
            service._read_attachment_data_unlocked(
                "account", "INBOX", "42", 0
            )

        self.assertEqual(ctx.exception.message_uid, "42")
        self.assertEqual(ctx.exception.reason, MessageUnavailableReason.VANISHED)


if __name__ == "__main__":
    unittest.main()
