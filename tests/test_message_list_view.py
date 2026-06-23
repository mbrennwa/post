# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.message_list_view import VirtualMessageList


def _msg(uid: str, *, seen: bool = True, flagged: bool = False) -> dict:
    return {
        "uid": uid,
        "subject": f"Message {uid}",
        "from": "sender@example.com",
        "flags": {"seen": seen, "flagged": flagged},
    }


class UpdateMessageFlagsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.message_list = VirtualMessageList()
        self.message_list.set_messages([_msg("1", seen=False, flagged=True)], folder_name="INBOX")

    def test_update_message_flags_merges_partial_flags(self) -> None:
        self.message_list.update_message_flags("1", {"seen": True})

        message = self.message_list.get_message("1")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message["flags"], {"seen": True, "flagged": True})

    def test_update_message_flags_toggles_seen_and_flagged(self) -> None:
        self.message_list.update_message_flags("1", {"seen": True})
        self.message_list.update_message_flags("1", {"flagged": False})

        message = self.message_list.get_message("1")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message["flags"], {"seen": True, "flagged": False})

    def test_update_message_flags_ignores_unknown_uid(self) -> None:
        self.message_list.update_message_flags("missing", {"seen": False})

        message = self.message_list.get_message("1")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message["flags"], {"seen": False, "flagged": True})


if __name__ == "__main__":
    unittest.main()
