# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ctypes
import os
import unittest
from unittest import mock

from post.mail.camel_util import (
    _UID_B64_PREFIX,
    _GPtrArray,
    _decode_uid_bytes,
    _read_ptr_array_uids,
    camel_uid_to_api,
    camel_uid_to_bytes,
    folder_get_message_info,
    folder_get_uids,
    folder_get_unread_count,
    folder_search_uids,
)


class _LegacyFolder:
    """Stub with pre-3.58 Camel Folder UID / unread APIs."""

    def __init__(self, uids=None, unread=0, *, fail_utf8: bool = False):
        self._uids = uids if uids is not None else ["1", "2"]
        self._unread = unread
        self._fail_utf8 = fail_utf8

    def get_uids(self):
        if self._fail_utf8:
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte"
            )
        return list(self._uids)

    def get_unread_message_count(self):
        return self._unread


class _ModernFolder:
    """Stub with EDS ≥3.58 Camel Folder UID / unread APIs."""

    def __init__(self, uids=None, unread=0, *, fail_utf8: bool = False):
        self._uids = uids if uids is not None else ["9", "10"]
        self._unread = unread
        self._fail_utf8 = fail_utf8

    def dup_uids(self):
        if self._fail_utf8:
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte"
            )
        return list(self._uids)

    def get_folder_summary(self):
        unread = self._unread

        class _Summary:
            def get_unread_count(self_inner):
                return unread

        return _Summary()


class CamelUidEncodingTests(unittest.TestCase):
    def test_ascii_uid_round_trip(self) -> None:
        self.assertEqual(_decode_uid_bytes(b"12345"), "12345")
        self.assertEqual(camel_uid_to_api("12345"), "12345")
        self.assertEqual(camel_uid_to_bytes("12345"), b"12345")

    def test_binary_uid_uses_base64_prefix(self) -> None:
        raw = b"\xa0\xff\x90"
        uid = _decode_uid_bytes(raw)
        self.assertIsNotNone(uid)
        assert uid is not None
        self.assertTrue(uid.startswith(_UID_B64_PREFIX))
        self.assertEqual(camel_uid_to_bytes(uid), raw)
        with self.assertRaises(TypeError):
            camel_uid_to_api(uid)

    def test_rejects_imap_token_garbage(self) -> None:
        self.assertIsNone(_decode_uid_bytes(b"LIST"))
        self.assertIsNone(_decode_uid_bytes(b"QUOTA"))
        self.assertIsNone(_decode_uid_bytes(b""))


class FolderGetUidsTests(unittest.TestCase):
    def test_uses_get_uids_when_utf8_safe(self) -> None:
        self.assertEqual(folder_get_uids(_LegacyFolder(["1", "2"])), ["1", "2"])

    def test_uses_dup_uids_on_modern_camel(self) -> None:
        self.assertEqual(folder_get_uids(_ModernFolder(["9", "10"])), ["9", "10"])

    def test_falls_back_when_get_uids_is_not_utf8(self) -> None:
        folder = _LegacyFolder(fail_utf8=True)
        with mock.patch(
            "post.mail.camel_util._folder_uids_via_ctypes",
            return_value=[f"{_UID_B64_PREFIX}ov8="],
        ) as fallback:
            self.assertEqual(folder_get_uids(folder), [f"{_UID_B64_PREFIX}ov8="])
        fallback.assert_called_once_with(folder)

    def test_falls_back_when_dup_uids_is_not_utf8(self) -> None:
        folder = _ModernFolder(fail_utf8=True)
        with mock.patch(
            "post.mail.camel_util._folder_uids_via_ctypes",
            return_value=[f"{_UID_B64_PREFIX}ov8="],
        ) as fallback:
            self.assertEqual(folder_get_uids(folder), [f"{_UID_B64_PREFIX}ov8="])
        fallback.assert_called_once_with(folder)


class FolderGetUnreadCountTests(unittest.TestCase):
    def test_legacy_get_unread_message_count(self) -> None:
        self.assertEqual(folder_get_unread_count(_LegacyFolder(unread=3)), 3)

    def test_modern_folder_summary_unread(self) -> None:
        self.assertEqual(folder_get_unread_count(_ModernFolder(unread=5)), 5)

    def test_mock_legacy_return_value(self) -> None:
        folder = mock.Mock()
        folder.get_unread_message_count.return_value = 4
        self.assertEqual(folder_get_unread_count(folder), 4)

    def test_magicmock_legacy_return_value(self) -> None:
        folder = mock.MagicMock()
        folder.get_unread_message_count.return_value = 0
        self.assertEqual(folder_get_unread_count(folder), 0)

    def test_unconfigured_mock_returns_unknown(self) -> None:
        self.assertEqual(folder_get_unread_count(mock.Mock()), -1)
        self.assertEqual(folder_get_unread_count(mock.MagicMock()), -1)


class FolderGetMessageInfoTests(unittest.TestCase):
    def test_uses_ctypes_for_binary_uid(self) -> None:
        folder = mock.Mock()
        uid = f"{_UID_B64_PREFIX}kKA="
        info = object()
        with mock.patch(
            "post.mail.camel_util._folder_get_message_info_via_ctypes",
            return_value=info,
        ) as lookup:
            self.assertIs(folder_get_message_info(folder, uid), info)
        lookup.assert_called_once_with(folder, b"\x90\xa0")
        folder.get_message_info.assert_not_called()

    def test_uses_gi_for_numeric_uid(self) -> None:
        folder = mock.Mock()
        folder_get_message_info(folder, "12345")
        folder.get_message_info.assert_called_once_with("12345")


class FolderSearchUidsTests(unittest.TestCase):
    def test_uses_ctypes_folder_search_and_frees_result(self) -> None:
        folder = mock.Mock()
        lib = mock.Mock()
        lib.camel_folder_search_by_expression.return_value = ctypes.c_void_p(0x1)

        with (
            mock.patch("post.mail.camel_util._get_libcamel", return_value=lib),
            mock.patch("post.mail.camel_util._gobject_pointer", return_value=ctypes.c_void_p(1)),
            mock.patch(
                "post.mail.camel_util._read_ptr_array_uids",
                return_value=["1"],
            ) as read_uids,
        ):
            result = folder_search_uids(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                ["1", "2"],
            )

        self.assertEqual(result, ["1"])
        read_uids.assert_called_once()
        lib.camel_folder_search_free.assert_called_once()

    def test_maps_numeric_match_to_index_uid(self) -> None:
        folder = mock.Mock()
        lib = mock.Mock()
        lib.camel_folder_search_by_expression.return_value = ctypes.c_void_p(0x1)

        with (
            mock.patch("post.mail.camel_util._get_libcamel", return_value=lib),
            mock.patch("post.mail.camel_util._gobject_pointer", return_value=ctypes.c_void_p(1)),
            mock.patch(
                "post.mail.camel_util._read_ptr_array_uids",
                return_value=["42"],
            ),
        ):
            result = folder_search_uids(
                folder,
                '(match-all (header-contains "Subject" "Core"))',
                ["0042", "99"],
            )

        self.assertEqual(result, ["0042"])

    def test_skips_binary_uids_in_scope_list(self) -> None:
        folder = mock.Mock()
        lib = mock.Mock()
        lib.camel_folder_search_by_expression.return_value = ctypes.c_void_p(0x1)

        binary_uid = f"{_UID_B64_PREFIX}kKA="
        with (
            mock.patch("post.mail.camel_util._get_libcamel", return_value=lib),
            mock.patch("post.mail.camel_util._gobject_pointer", return_value=ctypes.c_void_p(1)),
            mock.patch(
                "post.mail.camel_util._read_ptr_array_uids",
                return_value=["2"],
            ),
        ):
            result = folder_search_uids(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                [binary_uid, "2"],
            )

        self.assertEqual(result, ["2"])


class FolderSearchIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("POST_EDS_TESTS"),
        "Set POST_EDS_TESTS=1 to run EDS integration tests",
    )
    def test_search_inbox_when_available(self) -> None:
        try:
            import gi

            gi.require_version("EDataServer", "1.2")
            gi.require_version("Camel", "1.2")
            from gi.repository import Camel, EDataServer

            from post.mail.eds import MailService
            from post.mail.search import MessageSearchQuery, SearchTerm
        except (ImportError, ValueError) as exc:
            self.skipTest(str(exc))

        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            self.skipTest("EDS registry unavailable")

        service = MailService.connect()
        accounts = service.list_accounts()
        if not accounts:
            self.skipTest("No mail accounts configured")

        account = accounts[0]
        folders = service.list_folders(account.uid)
        inbox = next(
            (
                folder["full_name"]
                for folder in folders
                if (folder.get("full_name") or "").upper() == "INBOX"
            ),
            folders[0]["full_name"] if folders else None,
        )
        if inbox is None:
            self.skipTest("No folders available")

        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="the"),))
        messages, match_count, total, source = service.search_folder_messages(
            account.uid,
            inbox,
            query,
            sync=False,
        )
        self.assertIsInstance(messages, list)
        self.assertGreaterEqual(match_count, 0)
        self.assertGreaterEqual(total, 0)
        self.assertIn(source, ("memory", "disk_cache", "local", "server"))

        messages2, match_count2, _total2, _source2 = service.search_folder_messages(
            account.uid,
            inbox,
            query,
            sync=False,
        )
        self.assertIsInstance(messages2, list)
        self.assertEqual(match_count2, match_count)


if __name__ == "__main__":
    unittest.main()
