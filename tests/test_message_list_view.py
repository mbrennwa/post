# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

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


class PrependMessagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.message_list = VirtualMessageList()
        self.message_list.set_messages(
            [_msg("2"), _msg("1")],
            folder_name="INBOX",
        )

    @staticmethod
    def _pump_main_context() -> None:
        context = GLib.MainContext.default()
        while context.iteration(False):
            pass

    def test_prepend_messages_inserts_at_front(self) -> None:
        self.message_list.prepend_messages([_msg("3")], folder_name="INBOX")

        self.assertEqual(self.message_list.item_count(), 3)
        self.assertIsNotNone(self.message_list.get_message("3"))
        self.assertEqual(self.message_list.get_message("2"), _msg("2"))

    def test_prepend_messages_preserves_selection(self) -> None:
        self.message_list.selection.select_item(1, False)

        self.message_list.prepend_messages([_msg("3")], folder_name="INBOX")

        self.assertTrue(self.message_list.selection.is_selected(2))

    def test_set_messages_at_top_schedules_scroll_to_top(self) -> None:
        scrolled = {"called": False}
        original = self.message_list._scroll_to_top_after_layout

        def track_scroll() -> None:
            scrolled["called"] = True
            original()

        self.message_list._scroll_to_top_after_layout = track_scroll
        self.message_list.set_messages([_msg("3"), _msg("2"), _msg("1")], folder_name="INBOX")
        self._pump_main_context()

        self.assertTrue(scrolled["called"])


if __name__ == "__main__":
    unittest.main()
