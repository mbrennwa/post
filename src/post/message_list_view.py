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

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from post.mail.dnd import MESSAGE_TRANSFER_MIME, MessageTransferPayload, encode_message_transfer
from post.mail.folders import is_post_outbox_folder
from post.wrap_label import WrappingLabel, configure_ellipsize_label
from post.mail.helpers import (
    format_message_list_date,
    message_has_attachments,
    message_is_flagged,
    message_is_unread,
)

OnSelectionChanged = Callable[[], None]
OnItemActivated = Callable[[str], None]
OnItemPressed = Callable[[str], None]
OnItemContextMenu = Callable[[str, Gtk.Widget, float, float], None]
SearchMetaLabelResolver = Callable[[dict[str, Any]], str | None]


def _list_scroll_to_flags() -> Gtk.ListScrollFlags:
    flags = Gtk.ListScrollFlags.FOCUS
    align_center = getattr(Gtk.ListScrollFlags, "ALIGN_CENTER", None)
    if align_center is not None:
        flags |= align_center
    return flags


def _list_pick_flags() -> Gtk.PickFlags:
    flags = getattr(Gtk.PickFlags, "DEFAULT", Gtk.PickFlags(0))
    insensitive = getattr(Gtk.PickFlags, "INSENSITIVE", None)
    if insensitive is not None:
        flags |= insensitive
    return flags


class MessageListItem(GObject.Object):
    __gtype_name__ = "MessageListItem"

    message = GObject.Property(type=object)

    def __init__(self, message: dict[str, Any]) -> None:
        super().__init__(message=message)

    @property
    def uid(self) -> str:
        msg = self.message
        if not isinstance(msg, dict):
            return ""
        return str(msg.get("uid") or "")

    @property
    def list_key(self) -> str:
        msg = self.message
        if not isinstance(msg, dict):
            return ""
        row_key = msg.get("_search_row_key")
        if row_key:
            return str(row_key)
        return str(msg.get("uid") or "")

    def set_message(self, message: dict[str, Any]) -> None:
        self.message = message


class VirtualMessageList(Gtk.ScrolledWindow):
    """Scrollable virtual list of mail header rows."""

    def __init__(self) -> None:
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)

        self._folder_name = ""
        self._list_key_positions: dict[str, int] = {}
        self._search_meta_label_resolver: SearchMetaLabelResolver | None = None
        self._on_selection_changed: OnSelectionChanged | None = None
        self._on_item_activated: OnItemActivated | None = None
        self._on_item_pressed: OnItemPressed | None = None
        self._on_item_context_menu: OnItemContextMenu | None = None
        self._restoring_selection = False
        self._drag_account_uid: str | None = None
        self._drag_folder_name: str | None = None

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
        self._setup_list_drag_source()
        self.set_child(self._list_view)

    def set_search_meta_label_resolver(
        self, resolver: SearchMetaLabelResolver | None
    ) -> None:
        self._search_meta_label_resolver = resolver

    def set_drag_context(
        self, account_uid: str | None, folder_name: str | None
    ) -> None:
        self._drag_account_uid = account_uid
        self._drag_folder_name = folder_name

    def _setup_list_drag_source(self) -> None:
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)

        def prepare(
            _source: Gtk.DragSource, _x: float, _y: float
        ) -> Gdk.ContentProvider | None:
            if not self._drag_account_uid or not self._drag_folder_name:
                return None
            if is_post_outbox_folder(self._drag_folder_name):
                return None
            uids = self.get_selected_uids()
            if not uids:
                return None
            payload = MessageTransferPayload(
                account_uid=self._drag_account_uid,
                source_folder=self._drag_folder_name,
                uids=tuple(uids),
            )
            return Gdk.ContentProvider.new_for_bytes(
                MESSAGE_TRANSFER_MIME,
                GLib.Bytes.new(encode_message_transfer(payload)),
            )

        drag_source.connect("prepare", prepare)
        self._list_view.add_controller(drag_source)

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
        on_item_pressed: OnItemPressed | None = None,
        on_item_context_menu: OnItemContextMenu | None = None,
    ) -> None:
        self._on_selection_changed = on_selection_changed
        self._on_item_activated = on_item_activated
        self._on_item_pressed = on_item_pressed
        self._on_item_context_menu = on_item_context_menu

    def set_restoring_selection(self, restoring: bool) -> None:
        self._restoring_selection = restoring

    def is_restoring_selection(self) -> bool:
        return self._restoring_selection

    def clear(self) -> None:
        self._list_key_positions.clear()
        self._selection.unselect_all()
        self._store.remove_all()

    def item_count(self) -> int:
        return self._store.get_n_items()

    def set_messages(self, messages: Iterable[dict[str, Any]], *, folder_name: str) -> None:
        at_top = self._is_scrolled_to_top()
        self._folder_name = folder_name
        items = [MessageListItem(message) for message in messages]
        self._list_key_positions.clear()
        self._selection.unselect_all()
        count = self._store.get_n_items()
        if count:
            self._store.splice(0, count, items)
        elif items:
            self._store.splice(0, 0, items)
        self._rebuild_list_key_positions()
        if at_top and self._store.get_n_items() > 0:
            self._scroll_to_top_after_layout()

    def prepend_messages(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        folder_name: str,
    ) -> None:
        items = [MessageListItem(message) for message in messages]
        if not items:
            return
        at_top = self._is_scrolled_to_top()
        self._folder_name = folder_name
        self._store.splice(0, 0, items)
        # Prepend shifts every index; full rebuild is simplest and uncommon.
        self._rebuild_list_key_positions()
        if at_top:
            self._scroll_to_top_after_layout()

    def append_messages(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        folder_name: str,
    ) -> None:
        items = [MessageListItem(message) for message in messages]
        if not items:
            return
        self._folder_name = folder_name
        position = self._store.get_n_items()
        self._store.splice(position, 0, items)
        # O(batch) — avoid full O(n) rebuild on every UI batch for large folders.
        for item in items:
            if item.list_key:
                self._list_key_positions[item.list_key] = position
            position += 1

    def remove_uids(self, uids: Iterable[str]) -> int:
        uid_set = set(uids)
        if not uid_set:
            return 0
        removed = 0
        position = self._store.get_n_items() - 1
        while position >= 0:
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem) and item.list_key in uid_set:
                self._store.remove(position)
                removed += 1
            position -= 1
        if removed:
            self._rebuild_list_key_positions()
        return removed

    def upsert_message(
        self,
        message: dict[str, Any],
        *,
        folder_name: str,
        replace_uid: str | None = None,
    ) -> None:
        uid = str(message.get("uid") or "")
        if not uid:
            return
        self._folder_name = folder_name
        message = dict(message)

        if replace_uid:
            replace_position = self._list_key_positions.get(replace_uid)
            if replace_position is not None:
                item = self._store.get_item(replace_position)
                if isinstance(item, MessageListItem):
                    item.set_message(message)
                    new_key = item.list_key
                    if replace_uid != new_key:
                        del self._list_key_positions[replace_uid]
                        self._list_key_positions[new_key] = replace_position
                    return

        position = self._list_key_positions.get(message.get("_search_row_key") or uid)
        if position is not None:
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem):
                item.set_message(message)
            return

        self.prepend_messages([message], folder_name=folder_name)

    def update_message_flags(self, uid: str, flags: dict[str, Any]) -> None:
        position = self._list_key_positions.get(uid)
        if position is None:
            return
        item = self._store.get_item(position)
        if not isinstance(item, MessageListItem):
            return
        message = dict(item.message)
        merged_flags = dict(message.get("flags") or {})
        merged_flags.update(flags)
        message["flags"] = merged_flags
        item.set_message(message)

    def get_message(self, uid: str) -> dict[str, Any] | None:
        position = self._list_key_positions.get(uid)
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
            if isinstance(item, MessageListItem) and item.list_key:
                uids.append(item.list_key)
        return uids

    def get_primary_selected_uid(self) -> str | None:
        uids = self.get_selected_uids()
        return uids[0] if len(uids) == 1 else None

    def select_uid(self, uid: str | None) -> bool:
        if not uid:
            self._selection.unselect_all()
            return False
        position = self._list_key_positions.get(uid)
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
        widget = self._list_view.pick(x, y, _list_pick_flags())
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

    def _is_scrolled_to_top(self, *, epsilon: float = 1.0) -> bool:
        adj = self.get_vadjustment()
        if adj is None:
            return True
        return adj.get_value() <= epsilon

    def _scroll_to_top_after_layout(self) -> None:
        def _do_scroll() -> bool:
            if self._store.get_n_items() > 0:
                self._list_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
            return False

        GLib.idle_add(_do_scroll)

    def _rebuild_list_key_positions(self) -> None:
        self._list_key_positions.clear()
        for position in range(self._store.get_n_items()):
            item = self._store.get_item(position)
            if isinstance(item, MessageListItem) and item.list_key:
                self._list_key_positions[item.list_key] = position

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
        if isinstance(item, MessageListItem) and item.list_key:
            self._on_item_activated(item.list_key)

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

        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        preview.set_margin_start(4)
        preview.set_margin_end(12)
        preview.set_margin_top(8)
        preview.set_margin_bottom(8)

        subject_label = WrappingLabel(xalign=0, wrap=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        subject_label.set_hexpand(True)
        subject_label.set_halign(Gtk.Align.FILL)
        preview.append(subject_label)

        date_label = Gtk.Label(xalign=0)
        date_label.add_css_class("dim-label")
        date_label.set_halign(Gtk.Align.START)
        preview.append(date_label)

        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta = Gtk.Label(xalign=0, ellipsize=3)
        configure_ellipsize_label(meta)
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

        outer.set_can_target(True)

        def on_row_primary_pressed(
            gesture: Gtk.GestureClick, n_press: int, x: float, y: float
        ) -> None:
            if n_press != 1:
                return
            uid = getattr(list_item, "message_uid", None)
            if not uid:
                return
            event = gesture.get_current_event()
            if (
                event is not None
                and Gdk.Event.triggers_context_menu(event)
                and self._on_item_context_menu is not None
            ):
                self._on_item_context_menu(uid, outer, x, y)
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                return
            modifiers = (
                event.get_modifier_state() if event is not None else Gdk.ModifierType(0)
            )
            if modifiers & (
                Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
            ):
                return
            if self._on_item_pressed is not None:
                self._on_item_pressed(uid)

        def on_row_context_pressed(
            gesture: Gtk.GestureClick, n_press: int, x: float, y: float
        ) -> None:
            if n_press != 1:
                return
            uid = getattr(list_item, "message_uid", None)
            if not uid or self._on_item_context_menu is None:
                return
            self._on_item_context_menu(uid, outer, x, y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        primary_gesture = Gtk.GestureClick()
        primary_gesture.set_button(Gdk.BUTTON_PRIMARY)
        primary_gesture.connect("pressed", on_row_primary_pressed)
        outer.add_controller(primary_gesture)

        context_gesture = Gtk.GestureClick()
        context_gesture.set_button(Gdk.BUTTON_SECONDARY)
        context_gesture.connect("pressed", on_row_context_pressed)
        outer.add_controller(context_gesture)

        list_item.set_child(outer)
        list_item.unread_dot = unread_dot
        list_item.subject_label = subject_label
        list_item.date_label = date_label
        list_item.meta_label = meta
        list_item.attach_icon = attach_icon
        list_item.flag_icon = flag_icon

    def _populate_list_item_row(
        self,
        list_item: Gtk.ListItem,
        store_item: MessageListItem,
    ) -> None:
        message = store_item.message
        if not isinstance(message, dict):
            return
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
            search_meta = None
            if self._search_meta_label_resolver is not None:
                search_meta = self._search_meta_label_resolver(message)
            meta_label.set_label(search_meta if search_meta else sender)

        unread_dot = list_item.unread_dot
        if isinstance(unread_dot, Gtk.Widget):
            unread_dot.set_visible(unread)

        attach_icon = list_item.attach_icon
        if isinstance(attach_icon, Gtk.Image):
            attach_icon.set_visible(message_has_attachments(message))

        flag_icon = list_item.flag_icon
        if isinstance(flag_icon, Gtk.Image):
            flag_icon.set_visible(message_is_flagged(message))

        list_item.message_uid = store_item.list_key
        list_item.message_flags = dict(flags)

    def _on_list_item_bind(self, _factory: Gtk.ListItemFactory, list_item: Gtk.ListItem) -> None:
        item = list_item.get_item()
        if not isinstance(item, MessageListItem):
            return
        self._populate_list_item_row(list_item, item)

        def on_message_changed(
            store_item: MessageListItem,
            _pspec: GObject.ParamSpec,
        ) -> None:
            self._populate_list_item_row(list_item, store_item)

        handler_id = item.connect("notify::message", on_message_changed)
        list_item.message_notify_item = item
        list_item.message_notify_handler_id = handler_id

    def _on_list_item_unbind(self, _factory: Gtk.ListItemFactory, list_item: Gtk.ListItem) -> None:
        store_item = getattr(list_item, "message_notify_item", None)
        handler_id = getattr(list_item, "message_notify_handler_id", None)
        if isinstance(store_item, MessageListItem) and handler_id is not None:
            store_item.disconnect(handler_id)
        list_item.message_notify_item = None
        list_item.message_notify_handler_id = None
        list_item.message_uid = None
        list_item.message_flags = {}
