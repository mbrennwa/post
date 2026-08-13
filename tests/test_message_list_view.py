# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from types import SimpleNamespace

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from post.message_list_view import MessageListItem, VirtualMessageList


def _msg(uid: str, *, seen: bool = True, flagged: bool = False) -> dict:
    return {
        "uid": uid,
        "subject": f"Message {uid}",
        "from": "sender@example.com",
        "flags": {"seen": seen, "flagged": flagged},
    }


def _fake_list_item() -> SimpleNamespace:
    return SimpleNamespace(
        subject_label=Gtk.Label(),
        date_label=Gtk.Label(),
        meta_label=Gtk.Label(),
        unread_dot=Gtk.Box(),
        attach_icon=Gtk.Image(),
        flag_icon=Gtk.Image(),
    )


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

    def test_set_messages_does_not_alias_source_flags_dict(self) -> None:
        source = _msg("1", flagged=False)
        self.message_list.set_messages([source], folder_name="INBOX")

        source["flags"]["flagged"] = True

        message = self.message_list.get_message("1")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertFalse(message["flags"]["flagged"])
        self.assertIsNot(message["flags"], source["flags"])

    def test_inplace_flag_mutation_does_not_refresh_flag_icon(self) -> None:
        """Document #289: mutating flags without set_message leaves the icon stale."""
        self.message_list.set_messages([_msg("1", flagged=False)], folder_name="INBOX")
        item = self.message_list._store.get_item(0)
        self.assertIsInstance(item, MessageListItem)
        assert isinstance(item, MessageListItem)

        list_item = _fake_list_item()
        self.message_list._populate_list_item_row(list_item, item)
        self.assertFalse(list_item.flag_icon.get_visible())

        item.message["flags"]["flagged"] = True
        self.assertTrue(item.message["flags"]["flagged"])
        self.assertFalse(list_item.flag_icon.get_visible())

    def test_update_message_flags_refreshes_flag_icon_visibility(self) -> None:
        self.message_list.set_messages([_msg("1", flagged=False)], folder_name="INBOX")
        item = self.message_list._store.get_item(0)
        self.assertIsInstance(item, MessageListItem)
        assert isinstance(item, MessageListItem)

        list_item = _fake_list_item()
        self.message_list._populate_list_item_row(list_item, item)

        def on_message_changed(
            store_item: MessageListItem,
            _pspec: object,
        ) -> None:
            self.message_list._populate_list_item_row(list_item, store_item)

        item.connect("notify::message", on_message_changed)
        self.assertFalse(list_item.flag_icon.get_visible())

        self.message_list.update_message_flags("1", {"flagged": True})
        self.assertTrue(list_item.flag_icon.get_visible())

        self.message_list.update_message_flags("1", {"flagged": False})
        self.assertFalse(list_item.flag_icon.get_visible())


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


class AppendMessagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.message_list = VirtualMessageList()
        self.message_list.set_messages([_msg("2"), _msg("1")], folder_name="INBOX")

    def test_append_messages_adds_at_end(self) -> None:
        self.message_list.append_messages([_msg("0")], folder_name="INBOX")

        self.assertEqual(self.message_list.item_count(), 3)
        self.assertIsNotNone(self.message_list.get_message("0"))
        self.assertIsNotNone(self.message_list.get_message("1"))

    def test_append_messages_preserves_order(self) -> None:
        self.message_list.append_messages([_msg("3"), _msg("4")], folder_name="INBOX")

        self.assertEqual(self.message_list.item_count(), 4)
        self.assertIsNotNone(self.message_list.get_message("3"))
        self.assertIsNotNone(self.message_list.get_message("4"))


class UpsertMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.message_list = VirtualMessageList()
        self.message_list.set_messages([_msg("1", seen=False)], folder_name="Drafts")

    def test_upsert_message_updates_subject_in_place(self) -> None:
        updated = {
            "uid": "1",
            "subject": "Updated subject",
            "from": "sender@example.com",
            "flags": {"seen": False, "flagged": False},
        }
        self.message_list.upsert_message(updated, folder_name="Drafts")

        message = self.message_list.get_message("1")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message["subject"], "Updated subject")

    def test_upsert_message_replaces_uid_in_place(self) -> None:
        replacement = {
            "uid": "2",
            "subject": "New draft",
            "from": "sender@example.com",
            "flags": {"seen": True, "flagged": False},
        }
        self.message_list.upsert_message(
            replacement,
            folder_name="Drafts",
            replace_uid="1",
        )

        self.assertIsNone(self.message_list.get_message("1"))
        self.assertEqual(self.message_list.get_message("2"), replacement)
        self.assertEqual(self.message_list.item_count(), 1)

    def test_upsert_message_replaces_search_row_key(self) -> None:
        from post.mail.search import annotate_search_match

        original = annotate_search_match(
            {
                "uid": "old",
                "subject": "Hi",
                "from": "a@b.c",
                "flags": {"seen": True, "flagged": False},
            },
            account_uid="acct",
            folder_name="Archive",
        )
        self.message_list.set_messages([original], folder_name="Archive")
        updated = annotate_search_match(
            {
                "uid": "new",
                "subject": "Hi",
                "from": "a@b.c",
                "flags": {"seen": True, "flagged": False},
            },
            account_uid="acct",
            folder_name="Archive",
        )
        self.message_list.upsert_message(
            updated,
            folder_name="Archive",
            replace_uid=original["_search_row_key"],
        )
        self.assertIsNone(self.message_list.get_message(original["_search_row_key"]))
        found = self.message_list.get_message(updated["_search_row_key"])
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["uid"], "new")
        self.assertEqual(self.message_list.item_count(), 1)


if __name__ == "__main__":
    unittest.main()
