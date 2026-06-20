# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest

from post.mail.local_delivery import (
    all_recipients_local,
    deliver_to_maildir,
    deliver_to_spool,
    is_local_recipient,
)


class LocalRecipientTests(unittest.TestCase):
    def test_localhost_is_local(self) -> None:
        self.assertTrue(
            is_local_recipient(
                "user@localhost",
                local_address="mbrennwa@localhost",
            )
        )

    def test_external_is_not_local(self) -> None:
        self.assertFalse(
            is_local_recipient(
                "user@gmail.com",
                local_address="mbrennwa@localhost",
            )
        )

    def test_all_recipients_must_be_local(self) -> None:
        self.assertTrue(
            all_recipients_local(
                to=["mbrennwa@localhost"],
                cc=["other@localhost"],
                bcc=None,
                local_address="mbrennwa@localhost",
            )
        )
        self.assertFalse(
            all_recipients_local(
                to=["mbrennwa@localhost"],
                cc=["other@gmail.com"],
                bcc=None,
                local_address="mbrennwa@localhost",
            )
        )


class LocalDeliveryTests(unittest.TestCase):
    def test_deliver_to_spool_appends_mbox_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool = os.path.join(tmp, "mbox")
            with open(spool, "wb") as handle:
                handle.write(b"")

            deliver_to_spool(
                spool,
                b"Subject: hello\n\nBody text\n",
                envelope_from="sender@localhost",
            )

            with open(spool, "rb") as handle:
                data = handle.read()
            self.assertIn(b"From sender@localhost", data)
            self.assertIn(b"Subject: hello", data)
            self.assertIn(b"Body text", data)

    def test_deliver_to_maildir_writes_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deliver_to_maildir(tmp, b"Subject: hi\n\nHello\n")
            new_dir = os.path.join(tmp, "new")
            self.assertTrue(os.path.isdir(new_dir))
            names = os.listdir(new_dir)
            self.assertEqual(len(names), 1)
            with open(os.path.join(new_dir, names[0]), "rb") as handle:
                self.assertIn(b"Subject: hi", handle.read())
