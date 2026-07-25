# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")

from gi.repository import Gtk

from post.preferences import MESSAGE_APPEARANCE_ADAPT_TEXT
from post.reader.pane import MessageReaderPane


def _noop(*_args: Any, **_kwargs: Any) -> None:
    pass


def _sample_message(*, seen: bool = True, flagged: bool = False) -> dict[str, Any]:
    return {
        "uid": "42",
        "subject": "Hello",
        "from": "sender@example.com",
        "date": "2026-01-01",
        "flags": {"seen": seen, "flagged": flagged},
        "attachments": [],
    }


class MessageReaderPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.pane = MessageReaderPane(
            on_read_toggle=_noop,
            on_flag_toggle=_noop,
            on_reply=_noop,
            on_reply_all=_noop,
            on_forward=_noop,
            on_unsubscribe=_noop,
            on_attachment_clicked=_noop,
            on_attachment_context_menu=_noop,
            on_open_uri=_noop,
        )

    def test_show_message_exposes_current_message(self) -> None:
        msg = _sample_message()
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertIs(self.pane.current_message, msg)

    def test_unsubscribe_button_hidden_by_default(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertFalse(self.pane._unsubscribe_btn.get_visible())

    def test_unsubscribe_button_visible_when_action_present(self) -> None:
        clicked: list[dict[str, str]] = []

        def on_unsubscribe(action: dict[str, str]) -> None:
            clicked.append(action)

        pane = MessageReaderPane(
            on_read_toggle=_noop,
            on_flag_toggle=_noop,
            on_reply=_noop,
            on_reply_all=_noop,
            on_forward=_noop,
            on_unsubscribe=on_unsubscribe,
            on_attachment_clicked=_noop,
            on_attachment_context_menu=_noop,
            on_open_uri=_noop,
        )
        msg = _sample_message()
        msg["unsubscribe"] = {
            "kind": "open",
            "url": "https://example.com/unsub",
        }
        pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertTrue(pane._unsubscribe_btn.get_visible())
        pane._unsubscribe_btn.emit("clicked")
        self.assertEqual(
            clicked,
            [{"kind": "open", "url": "https://example.com/unsub"}],
        )

    def test_unsubscribe_button_is_left_of_read(self) -> None:
        outer = self.pane._message_actions
        children: list[Gtk.Widget] = []
        child = outer.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        self.assertGreaterEqual(len(children), 2)
        self.assertIs(children[0], self.pane._unsubscribe_btn)
        flag_group = children[1]
        self.assertIs(flag_group.get_first_child(), self.pane._read_toggle_btn)

    def test_clear_resets_current_message(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.pane.clear()
        self.assertIsNone(self.pane.current_message)

    def test_update_message_flags_merges_flags(self) -> None:
        msg = _sample_message(seen=False, flagged=False)
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.pane.update_message_flags({"seen": True})
        assert self.pane.current_message is not None
        self.assertEqual(
            self.pane.current_message["flags"],
            {"seen": True, "flagged": False},
        )

    def test_show_loading_clears_current_message(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.pane.show_loading()
        self.assertIsNone(self.pane.current_message)
