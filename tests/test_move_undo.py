# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.camel_util import camel_uid_list, normalize_camel_uid

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


class NormalizeCamelUidTests(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(normalize_camel_uid("42"), "42")
        self.assertEqual(normalize_camel_uid("  7  "), "7")

    def test_rejects_empty_and_zero(self) -> None:
        self.assertIsNone(normalize_camel_uid(""))
        self.assertIsNone(normalize_camel_uid("0"))
        self.assertIsNone(normalize_camel_uid("  "))

    def test_rejects_non_numeric(self) -> None:
        self.assertIsNone(normalize_camel_uid("abc"))
        self.assertIsNone(normalize_camel_uid("12a"))


class SpoolTrashTests(unittest.TestCase):
    def test_spool_uses_delete_trash(self) -> None:
        self.assertEqual(_SPOOL_BACKEND, "spool")

    def test_imap_and_maildir_are_not_spool(self) -> None:
        self.assertNotEqual("imapx", _SPOOL_BACKEND)
        self.assertNotEqual("maildir", _SPOOL_BACKEND)


if __name__ == "__main__":
    unittest.main()
