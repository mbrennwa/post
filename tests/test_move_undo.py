# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.camel_util import (
    camel_uid_list,
    folder_get_uids,
    normalize_camel_uid,
)

_SPOOL_BACKEND = "spool"


class CamelUidListTests(unittest.TestCase):
    def test_none(self) -> None:
        self.assertEqual(camel_uid_list(None), [])

    def test_string(self) -> None:
        self.assertEqual(camel_uid_list("42"), ["42"])

    def test_sequence(self) -> None:
        self.assertEqual(camel_uid_list(["1", "2"]), ["1", "2"])

    def test_empty_string(self) -> None:
        self.assertEqual(camel_uid_list(""), [])


class FolderGetUidsTests(unittest.TestCase):
    def test_uses_get_uids_when_utf8_safe(self) -> None:
        folder = mock.Mock()
        folder.get_uids.return_value = ["1", "2"]
        self.assertEqual(folder_get_uids(folder), ["1", "2"])

    def test_falls_back_when_get_uids_is_not_utf8(self) -> None:
        folder = mock.Mock()
        folder.get_uids.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "invalid start byte"
        )
        with mock.patch(
            "post.mail.camel_util._folder_uids_via_ctypes",
            return_value=["uidb64:ov8="],
        ) as fallback:
            self.assertEqual(folder_get_uids(folder), ["uidb64:ov8="])
        fallback.assert_called_once_with(folder)


class NormalizeCamelUidTests(unittest.TestCase):
    def test_valid_numeric(self) -> None:
        self.assertEqual(normalize_camel_uid("42"), "42")
        self.assertEqual(normalize_camel_uid("  7  "), "7")

    def test_accepts_opaque_and_uidb64(self) -> None:
        self.assertEqual(normalize_camel_uid("abc"), "abc")
        self.assertEqual(normalize_camel_uid("12a"), "12a")
        self.assertEqual(
            normalize_camel_uid("AQMkADAwATM0MDAAMS0"),
            "AQMkADAwATM0MDAAMS0",
        )
        self.assertEqual(normalize_camel_uid("uidb64:ov8="), "uidb64:ov8=")
        self.assertEqual(
            normalize_camel_uid("  uidb64:ov8=  "),
            "uidb64:ov8=",
        )

    def test_rejects_empty_and_zero(self) -> None:
        self.assertIsNone(normalize_camel_uid(""))
        self.assertIsNone(normalize_camel_uid("0"))
        self.assertIsNone(normalize_camel_uid("  "))

    def test_rejects_invalid_uidb64(self) -> None:
        self.assertIsNone(normalize_camel_uid("uidb64:!!!"))


class SpoolTrashTests(unittest.TestCase):
    def test_spool_uses_delete_trash(self) -> None:
        self.assertEqual(_SPOOL_BACKEND, "spool")

    def test_imap_and_maildir_are_not_spool(self) -> None:
        self.assertNotEqual("imapx", _SPOOL_BACKEND)
        self.assertNotEqual("maildir", _SPOOL_BACKEND)


if __name__ == "__main__":
    unittest.main()
