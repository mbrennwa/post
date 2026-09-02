# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gio, Gtk, WebKit

from post.preferences import MESSAGE_APPEARANCE_ADAPT_TEXT
from post.reader.pane import (
    MessageReaderPane,
    apply_reader_link_hover,
    prepend_address_context_menu_items,
    reader_link_hover_origin,
    reader_link_tooltip_text,
    strip_reader_context_menu,
)


def _noop(*_args: Any, **_kwargs: Any) -> None:
    pass


def _menu_actions(menu: WebKit.ContextMenu) -> list[str | int]:
    actions: list[str | int] = []
    for item in menu.get_items():
        if item.is_separator():
            actions.append("SEP")
        else:
            stock = int(item.get_stock_action())
            if stock == int(WebKit.ContextMenuAction.CUSTOM):
                actions.append(item.get_title() or "CUSTOM")
            else:
                actions.append(stock)
    return actions


def _make_pane(
    *,
    on_unsubscribe: Any = _noop,
    on_add_to_calendar: Any = _noop,
    on_new_message_to: Any = _noop,
    on_search_messages_from: Any = _noop,
    can_search_messages: Any = None,
) -> MessageReaderPane:
    return MessageReaderPane(
        on_reply=_noop,
        on_reply_all=_noop,
        on_forward=_noop,
        on_unsubscribe=on_unsubscribe,
        on_add_to_calendar=on_add_to_calendar,
        on_attachment_clicked=_noop,
        on_attachment_context_menu=_noop,
        on_open_uri=_noop,
        on_new_message_to=on_new_message_to,
        on_search_messages_from=on_search_messages_from,
        can_search_messages=can_search_messages or (lambda: True),
    )


def _sample_message(*, seen: bool = True, flagged: bool = False) -> dict[str, Any]:
    return {
        "uid": "42",
        "subject": "Hello",
        "from": "Alice <sender@example.com>",
        "to": "Bob <bob@example.com>",
        "date_received": "2026-01-01 12:00:00",
        "flags": {"seen": seen, "flagged": flagged},
        "attachments": [],
    }


class MessageReaderPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.pane = _make_pane()

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
        self.assertTrue(self.pane._message_actions.get_visible())

    def test_show_message_can_hide_actions(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
            show_actions=False,
        )
        self.assertFalse(self.pane._message_actions.get_visible())

    def test_show_message_builds_interactive_address_rows(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertFalse(self.pane._reader_meta.get_visible())
        rows: list[Gtk.Widget] = []
        child = self.pane._reader_meta.get_next_sibling()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        self.assertGreaterEqual(len(rows), 3)
        labels = []
        for row in rows:
            field = row.get_first_child()
            assert field is not None
            labels.append(field.get_label())
        self.assertEqual(labels[0], "From:")
        self.assertIn("To:", labels)
        self.assertIn("Date:", labels)

    def test_address_menu_callbacks(self) -> None:
        composed: list[str] = []
        searched: list[str] = []
        copied: list[str] = []
        pane = _make_pane(
            on_new_message_to=composed.append,
            on_search_messages_from=searched.append,
        )
        pane.get_clipboard = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(set=lambda text: copied.append(text))
        )
        pane._context_address = "sender@example.com"
        pane._on_address_new_message_activate()
        pane._on_address_search_from_activate()
        pane._on_address_copy_activate()
        self.assertEqual(composed, ["sender@example.com"])
        self.assertEqual(searched, ["sender@example.com"])
        self.assertEqual(copied, ["sender@example.com"])

    def test_address_search_action_disabled_when_search_unavailable(self) -> None:
        pane = _make_pane(can_search_messages=lambda: False)
        pane._sync_address_search_action()
        self.assertFalse(pane._address_search_action.get_enabled())
        pane._can_search_messages = lambda: True
        pane._sync_address_search_action()
        self.assertTrue(pane._address_search_action.get_enabled())

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

        pane = _make_pane(on_unsubscribe=on_unsubscribe)
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

    def test_unsubscribe_button_is_left_of_reply(self) -> None:
        outer = self.pane._message_actions
        children: list[Gtk.Widget] = []
        child = outer.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        self.assertGreaterEqual(len(children), 3)
        self.assertIs(children[0], self.pane._unsubscribe_btn)
        self.assertIs(children[1], self.pane._toolbar_add_calendar_btn)
        reply_group = children[2]
        self.assertIs(reply_group.get_first_child(), self.pane._reply_btn)

    def test_calendar_invite_shows_full_link_and_add_button(self) -> None:
        long_url = (
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_VeryLongToken"
            "Abcdefghijklmnopqrstuvwxyz0123456789/0?context=%7b%22Tid%22%3a%22x%22%7d"
        )
        msg = _sample_message()
        msg["calendar_invite"] = {
            "title": "Standup with a rather long meeting title for ellipsis",
            "start": "2026-08-03T10:00:00",
            "end": "2026-08-03T10:30:00",
            "meeting_url": long_url,
        }
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertTrue(self.pane._invite_box.get_visible())
        self.assertTrue(self.pane._add_to_calendar_btn.get_visible())
        self.assertTrue(self.pane._toolbar_add_calendar_btn.get_visible())
        link_text = self.pane._invite_link.get_text()
        self.assertEqual(link_text, long_url)
        self.assertTrue(self.pane._invite_link_row.get_visible())
        self.assertNotIn("Link:", link_text)
        # Long URL must not inflate the reader pane's horizontal request.
        minimum, natural, _, _ = self.pane.measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)

    def test_invite_hides_mislabeled_ics_attachment(self) -> None:
        msg = _sample_message()
        msg["calendar_invite"] = {
            "title": "Reservation",
            "start": "2026-08-21T16:30:00",
            "attachment_index": 0,
        }
        msg["attachments"] = [
            {
                "index": 0,
                "filename": "reservation.ics",
                "mime_type": "application/octet-stream",
                "size": 128,
            },
            {
                "index": 1,
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size": 12,
            },
        ]
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertTrue(self.pane._invite_box.get_visible())
        self.assertTrue(self.pane._reader_attachments.get_visible())
        labels: list[str] = []
        list_column = self.pane._reader_attachments.get_last_child()
        assert list_column is not None
        btn = list_column.get_first_child()
        while btn is not None:
            row = btn.get_child()
            assert row is not None
            name_label = row.get_last_child()
            assert name_label is not None
            labels.append(name_label.get_label() or "")
            btn = btn.get_next_sibling()
        self.assertEqual(len(labels), 1)
        self.assertIn("notes.txt", labels[0])
        self.assertNotIn("reservation.ics", labels[0])

    def test_invite_link_menu_copies_url(self) -> None:
        url = "https://teams.microsoft.com/l/meetup-join/abc"
        copied: list[str] = []
        pane = _make_pane()
        pane.get_clipboard = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(set=lambda text: copied.append(text))
        )
        msg = _sample_message()
        msg["calendar_invite"] = {
            "title": "Standup",
            "meeting_url": url,
        }
        pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        pane._on_copy_invite_link()
        self.assertEqual(copied, [url])

    def test_clear_resets_current_message(self) -> None:
        msg = _sample_message()
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.pane.clear()
        self.assertIsNone(self.pane.current_message)

    def test_horizontal_measure_natural_at_least_minimum(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        for for_size in (-1, 124, 200, 649):
            minimum, natural, _, _ = self.pane.measure(
                Gtk.Orientation.HORIZONTAL, for_size
            )
            self.assertGreaterEqual(
                natural,
                minimum,
                f"for_size={for_size} natural={natural} min={minimum}",
            )

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

    def test_strip_reader_context_menu_drops_navigation_actions(self) -> None:
        menu = WebKit.ContextMenu.new()
        for action in (
            WebKit.ContextMenuAction.GO_BACK,
            WebKit.ContextMenuAction.GO_FORWARD,
            WebKit.ContextMenuAction.STOP,
            WebKit.ContextMenuAction.RELOAD,
            WebKit.ContextMenuAction.COPY,
            WebKit.ContextMenuAction.SELECT_ALL,
        ):
            menu.append(WebKit.ContextMenuItem.new_from_stock_action(action))

        strip_reader_context_menu(menu)

        self.assertEqual(
            _menu_actions(menu),
            [
                int(WebKit.ContextMenuAction.COPY),
                int(WebKit.ContextMenuAction.SELECT_ALL),
            ],
        )

    def test_strip_reader_context_menu_collapses_separators(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.GO_BACK
            )
        )
        menu.append(WebKit.ContextMenuItem.new_separator())
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(WebKit.ContextMenuAction.COPY)
        )
        menu.append(WebKit.ContextMenuItem.new_separator())
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.RELOAD
            )
        )
        menu.append(WebKit.ContextMenuItem.new_separator())

        strip_reader_context_menu(menu)

        self.assertEqual(
            _menu_actions(menu),
            [int(WebKit.ContextMenuAction.COPY)],
        )

    def test_web_view_context_menu_handler_strips_navigation(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.GO_BACK
            )
        )
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(WebKit.ContextMenuAction.COPY)
        )
        handled = self.pane._on_web_view_context_menu(
            self.pane._web_view,
            menu,
            WebKit.HitTestResult(),
        )
        self.assertFalse(handled)
        self.assertEqual(
            _menu_actions(menu),
            [int(WebKit.ContextMenuAction.COPY)],
        )

    def test_prepend_address_context_menu_items(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(WebKit.ContextMenuAction.COPY)
        )
        new_action = Gio.SimpleAction.new("address-new-message", None)
        search_action = Gio.SimpleAction.new("address-search-from", None)
        copy_action = Gio.SimpleAction.new("address-copy", None)
        prepend_address_context_menu_items(
            menu,
            new_message_action=new_action,
            search_from_action=search_action,
            copy_address_action=copy_action,
            email="sender@example.com",
        )
        self.assertEqual(
            _menu_actions(menu),
            [
                "New Message to sender@example.com…",
                "Search Messages from sender@example.com",
                "Copy address",
                "SEP",
                int(WebKit.ContextMenuAction.COPY),
            ],
        )

    def test_web_view_context_menu_adds_mailto_actions(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(WebKit.ContextMenuAction.COPY)
        )
        hit = MagicMock()
        hit.context_is_link.return_value = True
        hit.get_link_uri.return_value = "mailto:Sender%20%3Csender@example.com%3E"
        handled = self.pane._on_web_view_context_menu(
            self.pane._web_view,
            menu,
            hit,
        )
        self.assertFalse(handled)
        self.assertEqual(self.pane._context_address, "sender@example.com")
        self.assertEqual(
            _menu_actions(menu),
            [
                "New Message to sender@example.com…",
                "Search Messages from sender@example.com",
                "Copy address",
                "SEP",
                int(WebKit.ContextMenuAction.COPY),
            ],
        )

    def test_reader_link_tooltip_text_returns_href(self) -> None:
        hit = MagicMock()
        hit.context_is_link.return_value = True
        hit.get_link_uri.return_value = "https://example.com/renew"
        self.assertEqual(
            reader_link_tooltip_text(hit),
            "https://example.com/renew",
        )

    def test_reader_link_tooltip_text_passes_mailto(self) -> None:
        hit = MagicMock()
        hit.context_is_link.return_value = True
        hit.get_link_uri.return_value = "mailto:Sender%20%3Csender@example.com%3E"
        self.assertEqual(
            reader_link_tooltip_text(hit),
            "mailto:Sender%20%3Csender@example.com%3E",
        )

    def test_reader_link_tooltip_text_none_when_not_a_link(self) -> None:
        hit = MagicMock()
        hit.context_is_link.return_value = False
        hit.get_link_uri.return_value = "https://example.com/renew"
        self.assertIsNone(reader_link_tooltip_text(hit))

    def test_reader_link_tooltip_text_none_when_empty_href(self) -> None:
        hit = MagicMock()
        hit.context_is_link.return_value = True
        hit.get_link_uri.return_value = "  "
        self.assertIsNone(reader_link_tooltip_text(hit))
        self.assertIsNone(reader_link_tooltip_text(None))

    def test_mouse_target_changed_sets_and_clears_hover_url(self) -> None:
        link = MagicMock()
        link.context_is_link.return_value = True
        link.get_link_uri.return_value = "https://example.com/renew"
        self.pane._on_web_view_mouse_target_changed(self.pane._web_view, link, 0)
        self.assertTrue(self.pane._link_hover_box.get_visible())
        self.assertEqual(
            self.pane._link_hover_label.get_label(),
            "https://example.com/renew",
        )

        other = MagicMock()
        other.context_is_link.return_value = False
        other.get_link_uri.return_value = "https://example.com/renew"
        self.pane._on_web_view_mouse_target_changed(self.pane._web_view, other, 0)
        self.assertFalse(self.pane._link_hover_box.get_visible())
        self.assertEqual(self.pane._link_hover_label.get_label(), "")

    def test_positions_hover_chip_near_pointer(self) -> None:
        self.pane._link_hover_label.set_label("https://example.com/renew")
        self.pane._link_hover_box.set_visible(True)
        self.pane._position_link_hover((40, 50))
        self.assertEqual(self.pane._link_hover_box.get_margin_start(), 52)
        self.assertEqual(self.pane._link_hover_box.get_margin_top(), 66)

    def test_same_url_does_not_reposition_hover_chip(self) -> None:
        link = MagicMock()
        link.context_is_link.return_value = True
        link.get_link_uri.return_value = "https://example.com/renew"
        self.pane._on_web_view_mouse_target_changed(self.pane._web_view, link, 0)
        self.pane._position_link_hover((40, 50))
        self.pane._on_web_view_mouse_target_changed(self.pane._web_view, link, 0)
        self.assertEqual(self.pane._link_hover_box.get_margin_start(), 52)
        self.assertEqual(self.pane._link_hover_box.get_margin_top(), 66)

    def test_reader_link_hover_origin_stays_below_right(self) -> None:
        self.assertEqual(
            reader_link_hover_origin(40, 50, 80, 24, 400, 300),
            (52, 66),
        )

    def test_reader_link_hover_origin_clamps_without_horizontal_flip(self) -> None:
        x, y = reader_link_hover_origin(380, 280, 80, 24, 400, 300)
        self.assertEqual(x, 312)
        self.assertEqual(y, 240)
        x, _ = reader_link_hover_origin(40, 50, 350, 24, 400, 300)
        self.assertEqual(x, 42)

    def test_apply_reader_link_hover_skips_redundant_set(self) -> None:
        box = MagicMock()
        box.get_visible.return_value = True
        label = MagicMock()
        label.get_label.return_value = "https://example.com/renew"
        self.assertFalse(
            apply_reader_link_hover(box, label, "https://example.com/renew")
        )
        label.set_label.assert_not_called()
        box.set_visible.assert_not_called()
