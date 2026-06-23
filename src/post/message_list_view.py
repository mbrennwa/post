# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Virtualized GTK message list backed by Gio.ListStore + Gtk.ListView."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GObject, Gtk

from post.mail.folders import is_post_outbox_folder
from post.mail.helpers import (
    format_message_list_date,
    message_has_attachments,
    message_is_flagged,
    message_is_unread,
)

OnSelectionChanged = Callable[[], None]
OnItemActivated = Callable[[str], None]


def _list_scroll_to_flags() -> Gtk.ListScrollFlags:
    flags = Gtk.ListScrollFlags.FOCUS
    align_center = getattr(Gtk.ListScrollFlags, "ALIGN_CENTER", None)
    if align_center is not None:
        flags |= align_center
    return flags


class MessageListItem(GObject.Object):
    __gtype_name__ = "MessageListItem"

    def __init__(self, message: dict[str, Any]) -> None:
        super().__init__()
        self._message = message

    @property
    def message(self) -> dict[str, Any]:
        return self._message

    @property
    def uid(self) -> str:
        return str(self._message.get("uid") or "")

    def set_message(self, message: dict[str, Any]) -> None:
        self._message = message
        self.notify("message")


class VirtualMessageList(Gtk.ScrolledWindow):
    """Scrollable virtual list of mail header rows."""

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)

        self._folder_name = ""
        self._uid_positions: dict[str, int] = {}
        self._on_selection_changed: OnSelectionChanged | None = None
        self._on_item_activated: OnItemActivated | None = None
        self._restoring_selection = False

        self._store = Gio.ListStore(item_type=MessageListItem)
        self._selection = Gtk.MultiSelection.new(self._store)
        self._selection.connect("selection-changed", self._handle_selection_changed)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_list_item_setup)
        factory.connect("bind", self._on_list_item_bind)
        factory.connect("unbind", self._on_list_item_unbind)

        self._list_view = Gtk.ListView(model=self._selection, factory=factory)
        self._list_view.add_css_class("message-list")
        self._list_view.set_can_focus(True)
        self._list_view.connect("activate", self._on_list_view_activate)
        self.set_child(self._list_view)

    @property
    def list_view(self) -> Gtk.ListView:
        return self._list_view

    @property
    def selection(self) -> Gtk.MultiSelection:
        return self._selection

    def set_callbacks(
        self,
        *,
        on_selection_changed: OnSelectionChanged | None = None,
        on_item_activated: OnItemActivated | None = None,
    ) -> None:
        self._on_selection_changed = on_selection_changed
        self._on_item_activated = on_item_activated

    def set_restoring_selection(self, restoring: bool) -> None:
        self._restoring_selection = restoring

    def is_restoring_selection(self) -> bool:
        return self._restoring_selection

    def clear(self) -> None:
        self._uid_positions.clear()
        self._selection.unselect_all()
        self._store.remove_all()

    def item_count(self) -> int:
        return self._store.get_n_items()

    def set_messages(self, messages: Iterable[dict[str, Any]], *, folder_name: str) -> None:
        self._folder_name = folder_name
        items = [MessageListItem(message) for message in messages]
        self._uid_positions.clear()
        self._selection.unselect_all()
        count = self._store.get_n_items()
        if count:
            self._store.splice(0, count, items)
        elif items:
            self._store.splice(0, 0, items)
        self._rebuild_uid_positions()

    def remove_uids(self, uids: Iterable[str]) -> int:
        uid_set = set(uids)
        if not uid_set:
            return 0
        removed = 0
        position = self._store.get_n_items() - 1
        while position >= 0:
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem) and item.uid in uid_set:
                self._store.remove(position)
                removed += 1
            position -= 1
        if removed:
            self._rebuild_uid_positions()
        return removed

    def update_message_flags(self, uid: str, flags: dict[str, Any]) -> None:
        position = self._uid_positions.get(uid)
        if position is None:
            return
        item = self._store.get_item(position)
        if not isinstance(item, MessageListItem):
            return
        message = dict(item.message)
        message["flags"] = dict(flags)
        item.set_message(message)

    def get_message(self, uid: str) -> dict[str, Any] | None:
        position = self._uid_positions.get(uid)
        if position is None:
            return None
        item = self._store.get_item(position)
        if isinstance(item, MessageListItem):
            return item.message
        return None

    def get_selected_uids(self) -> list[str]:
        uids: list[str] = []
        for position in range(self._store.get_n_items()):
            if not self._selection.is_selected(position):
                continue
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem) and item.uid:
                uids.append(item.uid)
        return uids

    def get_primary_selected_uid(self) -> str | None:
        uids = self.get_selected_uids()
        return uids[0] if len(uids) == 1 else None

    def select_uid(self, uid: str | None) -> bool:
        if not uid:
            self._selection.unselect_all()
            return False
        position = self._uid_positions.get(uid)
        if position is None:
            self._selection.unselect_all()
            return False
        self._selection.unselect_all()
        self._selection.select_item(position, False)
        self._list_view.scroll_to(
            position,
            _list_scroll_to_flags(),
            None,
        )
        return True

    def pick_item_at(self, x: float, y: float) -> MessageListItem | None:
        widget = self._list_view.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None:
            if isinstance(widget, Gtk.ListItem):
                position = widget.get_position()
                if position == Gtk.INVALID_LIST_POSITION:
                    return None
                item = self._store.get_item(position)
                if isinstance(item, MessageListItem):
                    return item
                return None
            widget = widget.get_parent()
        return None

    def translate_to_scroll(self, x: float, y: float) -> tuple[float, float] | None:
        return self._list_view.translate_coordinates(self, x, y)

    def _rebuild_uid_positions(self) -> None:
        self._uid_positions.clear()
        for position in range(self._store.get_n_items()):
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem) and item.uid:
                self._uid_positions[item.uid] = position

    def _handle_selection_changed(
        self,
        _model: Gtk.SelectionModel,
        _position: int,
        _n_items: int,
    ) -> None:
        if self._on_selection_changed is not None:
            self._on_selection_changed()

    def _on_list_view_activate(
        self,
        _list_view: Gtk.ListView,
        position: int,
    ) -> None:
        if self._restoring_selection or self._on_item_activated is None:
            return
        item = self._store.get_item(position)
        if isinstance(item, MessageListItem) and item.uid:
            self._on_item_activated(item.uid)

    @staticmethod
    def _make_unread_dot(*, visible: bool) -> Gtk.Box:
        dot = Gtk.Box()
        dot.add_css_class("message-unread-dot")
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_visible(visible)
        return dot

    def _on_list_item_setup(self, _factory: Gtk.ListItemFactory, list_item: Gtk.ListItem) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_margin_start(8)
        outer.set_margin_end(0)

        dot_column = Gtk.Box()
        dot_column.set_size_request(16, -1)
        dot_column.set_valign(Gtk.Align.CENTER)
        unread_dot = self._make_unread_dot(visible=False)
        dot_column.append(unread_dot)
        outer.append(dot_column)

        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        preview.set_margin_start(4)
        preview.set_margin_end(12)
        preview.set_margin_top(8)
        preview.set_margin_bottom(8)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        subject_label = Gtk.Label(xalign=0, wrap=True)
        subject_label.set_hexpand(True)
        top_row.append(subject_label)

        date_label = Gtk.Label(xalign=1)
        date_label.add_css_class("dim-label")
        top_row.append(date_label)
        preview.append(top_row)

        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta = Gtk.Label(xalign=0, ellipsize=3)
        meta.set_hexpand(True)
        meta.add_css_class("dim-label")
        bottom_row.append(meta)

        attach_icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
        attach_icon.add_css_class("dim-label")
        attach_icon.set_tooltip_text("Has Attachments")
        attach_icon.set_visible(False)
        bottom_row.append(attach_icon)

        flag_icon = Gtk.Image.new_from_icon_name("mail-flag-symbolic")
        flag_icon.add_css_class("message-flagged-icon")
        flag_icon.set_tooltip_text("Flagged")
        flag_icon.set_visible(False)
        bottom_row.append(flag_icon)
        preview.append(bottom_row)
        outer.append(preview)

        list_item.set_child(outer)
        list_item.unread_dot = unread_dot
        list_item.subject_label = subject_label
        list_item.date_label = date_label
        list_item.meta_label = meta
        list_item.attach_icon = attach_icon
        list_item.flag_icon = flag_icon

    def _on_list_item_bind(self, _factory: Gtk.ListItemFactory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        if not isinstance(item, MessageListItem):
            return
        message = item.message
        folder_name = self._folder_name
        subject = message.get("subject") or "(no subject)"
        if is_post_outbox_folder(folder_name):
            sender = message.get("preview_to") or message.get("to") or ""
        else:
            sender = message.get("from") or ""
        unread = message_is_unread(message)
        flags = message.get("flags") or {}

        subject_label = list_item.subject_label
        if isinstance(subject_label, Gtk.Label):
            subject_label.set_label(subject)

        date_label = list_item.date_label
        date_text = format_message_list_date(message)
        if isinstance(date_label, Gtk.Label):
            if date_text:
                date_label.set_label(date_text)
                date_label.set_visible(True)
            else:
                date_label.set_visible(False)

        meta_label = list_item.meta_label
        if isinstance(meta_label, Gtk.Label):
            meta_label.set_label(sender)

        unread_dot = list_item.unread_dot
        if isinstance(unread_dot, Gtk.Widget):
            unread_dot.set_visible(unread)

        attach_icon = list_item.attach_icon
        if isinstance(attach_icon, Gtk.Image):
            attach_icon.set_visible(message_has_attachments(message))

        flag_icon = list_item.flag_icon
        if isinstance(flag_icon, Gtk.Image):
            flag_icon.set_visible(message_is_flagged(message))

        list_item.message_uid = item.uid
        list_item.message_flags = dict(flags)

    def _on_list_item_unbind(self, _factory: Gtk.ListItemFactory, list_item: Gtk.ListItem) -> None:
        list_item.message_uid = None
        list_item.message_flags = {}
