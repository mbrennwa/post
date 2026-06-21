# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.camel_util import camel_uid_list

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


class SpoolTrashTests(unittest.TestCase):
    def test_spool_uses_delete_trash(self) -> None:
        self.assertEqual(_SPOOL_BACKEND, "spool")

    def test_imap_and_maildir_are_not_spool(self) -> None:
        self.assertNotEqual("imapx", _SPOOL_BACKEND)
        self.assertNotEqual("maildir", _SPOOL_BACKEND)


if __name__ == "__main__":
    unittest.main()
