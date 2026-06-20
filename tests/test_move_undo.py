# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.eds import MailService


class CamelUidListTests(unittest.TestCase):
    def test_none(self) -> None:
        self.assertEqual(MailService._camel_uid_list(None), [])

    def test_string(self) -> None:
        self.assertEqual(MailService._camel_uid_list("42"), ["42"])

    def test_sequence(self) -> None:
        self.assertEqual(MailService._camel_uid_list(["1", "2"]), ["1", "2"])

    def test_empty_string(self) -> None:
        self.assertEqual(MailService._camel_uid_list(""), [])


if __name__ == "__main__":
    unittest.main()
