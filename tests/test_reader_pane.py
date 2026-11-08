# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gio, Gtk, Pango, WebKit

from post.preferences import MESSAGE_APPEARANCE_ADAPT_TEXT
from post.reader.image_menu import (
    LABEL_COPY_ADDRESS,
    LABEL_COPY_LINK_ADDRESS,
    LABEL_COPY_PICTURE,
    LABEL_OPEN_LINK,
    LABEL_OPEN_PICTURE,
    LABEL_SAVE_PICTURE,
)
from post.reader.pane import (
    RECIPIENT_HEADER_FALLBACK_LINE_PX,
    RECIPIENT_HEADER_SCROLL_LINES,
    MessageReaderPane,
    apply_reader_link_hover,
    prepend_address_context_menu_items,
    reader_link_hover_origin,
    reader_link_tooltip_text,
    recipient_header_scroll_max_height,
    recipient_header_visible_height,
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


def _header_row_widgets(pane: MessageReaderPane) -> list[Gtk.Widget]:
    rows: list[Gtk.Widget] = []
    child = pane._reader_meta.get_next_sibling()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def _header_row_named(pane: MessageReaderPane, name: str) -> Gtk.Widget:
    for row in _header_row_widgets(pane):
        label = _header_label(row)
        if label is not None and label.get_label().startswith(f"{name}:"):
            return row
    raise AssertionError(f"missing header row {name}")


def _header_label(row: Gtk.Widget) -> Gtk.Label | None:
    widget: Gtk.Widget | None = row
    if isinstance(widget, Gtk.ScrolledWindow):
        widget = widget.get_child()
    if isinstance(widget, Gtk.Viewport):
        widget = widget.get_child()
    return widget if isinstance(widget, Gtk.Label) else None


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

    def test_show_message_builds_header_rows(self) -> None:
        self.pane.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        self.assertFalse(self.pane._reader_meta.get_visible())
        rows = _header_row_widgets(self.pane)
        self.assertGreaterEqual(len(rows), 3)
        labels: list[str] = []
        for row in rows:
            label = _header_label(row)
            if label is not None:
                labels.append(label.get_label())
        self.assertTrue(any(lab.startswith("From:") for lab in labels))
        self.assertTrue(any(lab.startswith("To:") for lab in labels))
        self.assertTrue(any(lab.startswith("Date:") for lab in labels))

    def test_recipient_header_scroll_max_height_is_three_lines(self) -> None:
        self.assertEqual(recipient_header_scroll_max_height(20), 60)
        self.assertEqual(recipient_header_scroll_max_height(1), 3)
        self.assertEqual(
            recipient_header_scroll_max_height(0), RECIPIENT_HEADER_SCROLL_LINES
        )
        self.assertEqual(
            recipient_header_scroll_max_height(-5), RECIPIENT_HEADER_SCROLL_LINES
        )

    def test_recipient_header_visible_height_covers_third_line(self) -> None:
        label = Gtk.Label()
        layout = label.create_pango_layout("word " * 80)
        layout.set_width(120 * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        self.assertGreater(layout.get_line_count(), RECIPIENT_HEADER_SCROLL_LINES)
        height = recipient_header_visible_height(layout)
        iterator = layout.get_iter()
        y1 = 0
        for _ in range(RECIPIENT_HEADER_SCROLL_LINES):
            _y0, y1 = iterator.get_line_yrange()
            iterator.next_line()
        self.assertEqual(height, math.ceil(y1 / Pango.SCALE))
        truncated = (int(y1) // Pango.SCALE)
        self.assertGreaterEqual(height, truncated)
        self.assertLess(height, layout.get_pixel_size()[1])

        short = label.create_pango_layout("one line")
        self.assertEqual(
            recipient_header_visible_height(short),
            short.get_pixel_size()[1],
        )

    def test_recipient_headers_scroll_other_fields_do_not(self) -> None:
        msg = _sample_message()
        msg["reply_to"] = "Other <other@example.com>"
        msg["cc"] = "Carol <carol@example.com>"
        msg["bcc"] = "Dave <dave@example.com>"
        self.pane.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        rows: dict[str, Gtk.Widget] = {}
        for row in _header_row_widgets(self.pane):
            label = _header_label(row)
            self.assertIsNotNone(label)
            assert label is not None
            name = label.get_label().split(":", 1)[0]
            rows[name] = row
        for name in ("From", "Reply-To", "Date"):
            self.assertIsInstance(rows[name], Gtk.Label)
        expected_cap = recipient_header_scroll_max_height(
            RECIPIENT_HEADER_FALLBACK_LINE_PX
        )
        for name in ("To", "Cc", "Bcc"):
            scroll = rows[name]
            self.assertIsInstance(scroll, Gtk.ScrolledWindow)
            h_policy, v_policy = scroll.get_policy()
            self.assertEqual(h_policy, Gtk.PolicyType.NEVER)
            self.assertEqual(v_policy, Gtk.PolicyType.AUTOMATIC)
            self.assertTrue(scroll.get_propagate_natural_height())
            self.assertFalse(scroll.get_propagate_natural_width())
            self.assertFalse(scroll.get_vexpand())
            self.assertFalse(scroll.get_overlay_scrolling())
            self.assertEqual(scroll.get_max_content_height(), expected_cap)

    def test_recipient_header_height_follows_text_until_three_lines(self) -> None:
        short = _make_pane()
        short.show_message(
            _sample_message(),
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        short_to = _header_row_named(short, "To")
        short_label = _header_label(short_to)
        self.assertIsNotNone(short_label)
        assert short_label is not None
        width = 480
        label_height = short_label.measure(Gtk.Orientation.VERTICAL, width)[1]
        scroll_min, scroll_nat, *_rest = short_to.measure(Gtk.Orientation.VERTICAL, width)
        self.assertEqual(scroll_nat, label_height)
        self.assertLessEqual(scroll_min, label_height)

        long = _make_pane()
        msg = _sample_message()
        msg["to"] = ", ".join(f"Person {i} <p{i}@example.com>" for i in range(80))
        long.show_message(
            msg,
            body={"plain": "Body text", "html": None},
            allow_remote=False,
            dark=False,
            message_appearance=MESSAGE_APPEARANCE_ADAPT_TEXT,
        )
        long_to = _header_row_named(long, "To")
        long_label = _header_label(long_to)
        self.assertIsNotNone(long_label)
        assert long_label is not None
        cap = long_to.get_max_content_height()
        self.assertGreater(long_label.measure(Gtk.Orientation.VERTICAL, width)[1], cap)
        long_min, long_nat, *_rest = long_to.measure(Gtk.Orientation.VERTICAL, width)
        self.assertEqual(long_nat, cap)
        self.assertLessEqual(long_min, cap)

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

    def test_web_view_context_menu_embedded_image(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW
            )
        )
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD
            )
        )
        hit = MagicMock()
        hit.context_is_image.return_value = True
        hit.context_is_link.return_value = False
        hit.get_image_uri.return_value = "data:image/png;base64,aaaa"
        hit.get_link_uri.return_value = ""
        handled = self.pane._on_web_view_context_menu(
            self.pane._web_view, menu, hit
        )
        self.assertFalse(handled)
        self.assertEqual(self.pane._context_image_uri, "data:image/png;base64,aaaa")
        self.assertEqual(
            _menu_actions(menu),
            [LABEL_OPEN_PICTURE, LABEL_SAVE_PICTURE, LABEL_COPY_PICTURE],
        )

    def test_web_view_context_menu_remote_image(self) -> None:
        menu = WebKit.ContextMenu.new()
        hit = MagicMock()
        hit.context_is_image.return_value = True
        hit.context_is_link.return_value = False
        hit.get_image_uri.return_value = "https://cdn.example/logo.png"
        hit.get_link_uri.return_value = ""
        self.pane._on_web_view_context_menu(self.pane._web_view, menu, hit)
        self.assertEqual(
            _menu_actions(menu),
            [LABEL_OPEN_PICTURE, LABEL_SAVE_PICTURE, LABEL_COPY_ADDRESS],
        )
        self.assertNotIn(LABEL_COPY_PICTURE, _menu_actions(menu))

    def test_web_view_context_menu_linked_image(self) -> None:
        menu = WebKit.ContextMenu.new()
        menu.append(
            WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.OPEN_LINK
            )
        )
        hit = MagicMock()
        hit.context_is_image.return_value = True
        hit.context_is_link.return_value = True
        hit.get_image_uri.return_value = "data:image/png;base64,aaaa"
        hit.get_link_uri.return_value = "https://shop.example/campaign"
        self.pane._on_web_view_context_menu(self.pane._web_view, menu, hit)
        self.assertEqual(self.pane._context_link_uri, "https://shop.example/campaign")
        self.assertEqual(
            _menu_actions(menu),
            [
                LABEL_OPEN_PICTURE,
                LABEL_SAVE_PICTURE,
                LABEL_COPY_PICTURE,
                "SEP",
                LABEL_OPEN_LINK,
                LABEL_COPY_LINK_ADDRESS,
            ],
        )

    def test_web_view_context_menu_mailto_wrapped_image(self) -> None:
        menu = WebKit.ContextMenu.new()
        hit = MagicMock()
        hit.context_is_image.return_value = True
        hit.context_is_link.return_value = True
        hit.get_image_uri.return_value = "data:image/png;base64,aaaa"
        hit.get_link_uri.return_value = "mailto:sender@example.com"
        self.pane._on_web_view_context_menu(self.pane._web_view, menu, hit)
        self.assertEqual(self.pane._context_address, "sender@example.com")
        self.assertEqual(
            _menu_actions(menu),
            [
                LABEL_OPEN_PICTURE,
                LABEL_SAVE_PICTURE,
                LABEL_COPY_PICTURE,
                "SEP",
                "New Message to sender@example.com…",
                "Search Messages from sender@example.com",
                "Copy address",
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
