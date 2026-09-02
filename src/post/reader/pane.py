# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared message reader pane with WebKit body rendering."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Gio, Graphene, Gtk, WebKit

from post.mail.helpers import (
    ReaderHeaderRow,
    bare_email_from_address,
    format_attachment_size,
    mailto_primary_email,
    reader_header_rows,
)
from post.preferences import (
    MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
    MESSAGE_APPEARANCE_ADAPT_TEXT,
    MessageAppearance,
    get_load_remote_content,
    get_message_appearance,
)
from post.reader.html import build_reader_document
from post.wrap_label import (
    EllipsizingLabel,
    WrappingLabel,
    configure_ellipsize_label,
    set_label_wrap_mode,
)

# Browser chrome — meaningless for HTML loaded into the reading pane.
_READER_CONTEXT_MENU_BLOCKLIST = frozenset(
    {
        WebKit.ContextMenuAction.GO_BACK,
        WebKit.ContextMenuAction.GO_FORWARD,
        WebKit.ContextMenuAction.STOP,
        WebKit.ContextMenuAction.RELOAD,
    }
)


def strip_reader_context_menu(menu: WebKit.ContextMenu) -> None:
    """Remove WebKit navigation actions and tidy leftover separators."""
    for item in list(menu.get_items()):
        if item.is_separator():
            continue
        if item.get_stock_action() in _READER_CONTEXT_MENU_BLOCKLIST:
            menu.remove(item)

    items = list(menu.get_items())
    previous_was_separator = True  # drop leading separators too
    for item in items:
        if item.is_separator():
            if previous_was_separator:
                menu.remove(item)
            previous_was_separator = True
        else:
            previous_was_separator = False

    items = list(menu.get_items())
    if items and items[-1].is_separator():
        menu.remove(items[-1])


def reader_link_tooltip_text(
    hit_test_result: WebKit.HitTestResult | None,
) -> str | None:
    """Return the hovered link href for a tooltip, or ``None`` if not on a link.

    Uses the actual ``href``, not HTML ``title``, so a sender cannot spoof the
    destination with a misleading title attribute.
    """
    if hit_test_result is None or not hit_test_result.context_is_link():
        return None
    uri = (hit_test_result.get_link_uri() or "").strip()
    return uri or None


_LINK_HOVER_PAD = 8
_LINK_HOVER_OFFSET_X = 12
_LINK_HOVER_OFFSET_Y = 16


def apply_reader_link_hover(
    box: Gtk.Widget, label: Gtk.Label, text: str | None
) -> bool:
    """Show or hide the on-page hover URL overlay.

    GTK tooltips on WebKitGTK never appear: the view consumes pointer
    motion, so the tooltip hover timeout never fires. A non-targetable OSD
    overlay is updated from ``mouse-target-changed`` instead.

    Returns True only when the URL is newly shown or changed. Same-URL
    updates return False so the chip is not repositioned (nested markup
    inside a button would otherwise make it flicker).
    """
    if text:
        if box.get_visible() and label.get_label() == text:
            return False
        label.set_label(text)
        box.set_visible(True)
        return True
    if not box.get_visible() and not label.get_label():
        return False
    label.set_label("")
    box.set_visible(False)
    box.set_margin_start(0)
    box.set_margin_top(0)
    return False


def pointer_coords_in_widget(widget: Gtk.Widget) -> tuple[float, float] | None:
    """Return pointer coordinates in *widget* space, or ``None`` if unknown."""
    native = widget.get_native()
    if native is None or not isinstance(native, Gtk.Widget):
        return None
    surface = native.get_surface()
    if surface is None:
        return None
    seat = widget.get_display().get_default_seat()
    if seat is None:
        return None
    device = seat.get_pointer()
    if device is None:
        return None
    found, surface_x, surface_y, _mask = surface.get_device_position(device)
    if not found:
        return None
    origin_x, origin_y = native.get_surface_transform()
    native_point = Graphene.Point().init(surface_x - origin_x, surface_y - origin_y)
    ok, local = native.compute_point(widget, native_point)
    if not ok or local is None:
        return None
    return (local.x, local.y)


def reader_link_hover_origin(
    pointer_x: float,
    pointer_y: float,
    chip_w: float,
    chip_h: float,
    view_w: float,
    view_h: float,
) -> tuple[float, float]:
    """Place a hover chip below-right of the pointer, clamped to the view.

    Do not flip to the opposite side of the pointer. That jump, combined with
    a changing measured chip width, flickers left/right while hovering one
    button.
    """
    x = pointer_x + _LINK_HOVER_OFFSET_X
    y = pointer_y + _LINK_HOVER_OFFSET_Y
    if view_w <= 0 or view_h <= 0:
        return (x, y)
    pad = _LINK_HOVER_PAD
    if y + chip_h + pad > view_h:
        y = pointer_y - chip_h - _LINK_HOVER_OFFSET_Y
    max_x = view_w - chip_w - pad
    max_y = view_h - chip_h - pad
    x = min(max(x, pad), max(pad, max_x))
    y = min(max(y, pad), max(pad, max_y))
    return (x, y)


def prepend_address_context_menu_items(
    menu: WebKit.ContextMenu,
    *,
    new_message_action: Gio.Action,
    search_from_action: Gio.Action,
    copy_address_action: Gio.Action,
    email: str,
) -> None:
    """Prepend New Message / Search / Copy address actions for *email*."""
    had_items = bool(list(menu.get_items()))
    if had_items:
        menu.prepend(WebKit.ContextMenuItem.new_separator())
    # Prepend in reverse so visual order is New Message, Search, Copy.
    menu.prepend(
        WebKit.ContextMenuItem.new_from_gaction(
            copy_address_action,
            "Copy address",
        )
    )
    menu.prepend(
        WebKit.ContextMenuItem.new_from_gaction(
            search_from_action,
            f"Search Messages from {email}",
        )
    )
    menu.prepend(
        WebKit.ContextMenuItem.new_from_gaction(
            new_message_action,
            f"New Message to {email}…",
        )
    )


class _ClampingBoxLayout(Gtk.BoxLayout):
    """BoxLayout for MessageReaderPane.

    - Vertical: never report natural size below minimum (GTK height-for-width
      quirks with wrapping header + WebKit).
    - Horizontal: never let long URLs / WebKit force a huge minimum width that
      pushes the paned end child (and Add to Calendar) past the window edge.
    """

    __gtype_name__ = "PostClampingBoxLayout"

    def do_measure(
        self, widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 0, 0, -1, -1
        minimum, natural, min_baseline, nat_baseline = Gtk.BoxLayout.do_measure(
            self, widget, orientation, for_size
        )
        if natural < minimum:
            natural = minimum
        return minimum, natural, min_baseline, nat_baseline


class MessageReaderPane(Gtk.Box):
    """Inline or window reader: header, actions, attachments, and WebKit body."""

    __gtype_name__ = "MessageReaderPane"

    def __init__(
        self,
        *,
        on_reply: Callable[[], None],
        on_reply_all: Callable[[], None],
        on_forward: Callable[[], None],
        on_unsubscribe: Callable[[dict[str, str]], None],
        on_add_to_calendar: Callable[[dict[str, Any]], None],
        on_attachment_clicked: Callable[[int], None],
        on_attachment_context_menu: Callable[
            [Gtk.Widget, float, float, int, str | None, str], None
        ],
        on_open_uri: Callable[[str], None],
        on_new_message_to: Callable[[str], None],
        on_search_messages_from: Callable[[str], None],
        can_search_messages: Callable[[], bool],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        layout = _ClampingBoxLayout(orientation=Gtk.Orientation.VERTICAL)
        layout.set_spacing(8)
        self.set_layout_manager(layout)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self._on_reply = on_reply
        self._on_reply_all = on_reply_all
        self._on_forward = on_forward
        self._on_unsubscribe = on_unsubscribe
        self._on_add_to_calendar = on_add_to_calendar
        self._on_attachment_clicked = on_attachment_clicked
        self._on_attachment_context_menu = on_attachment_context_menu
        self._on_open_uri = on_open_uri
        self._on_new_message_to = on_new_message_to
        self._on_search_messages_from = on_search_messages_from
        self._can_search_messages = can_search_messages

        self._current_message: dict[str, Any] | None = None
        self._current_body: dict[str, str | None] = {"plain": None, "html": None}
        self._allow_remote = get_load_remote_content()
        self._message_appearance: MessageAppearance = get_message_appearance()
        self._dark = False
        self._context_address: str | None = None

        self._reader_subject = WrappingLabel(
            label="",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._reader_subject.add_css_class("title-2")
        self._reader_subject.set_max_width_chars(1)
        self._reader_subject.set_hexpand(True)
        self._reader_subject.set_halign(Gtk.Align.FILL)
        self._reader_subject.set_visible(False)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.set_hexpand(True)
        subject_box = Gtk.Box()
        subject_box.set_hexpand(True)
        subject_box.append(self._reader_subject)
        header_row.append(subject_box)
        self._message_actions = self._build_message_action_buttons()
        self._message_actions.set_valign(Gtk.Align.START)
        self._message_actions.set_halign(Gtk.Align.END)
        header_row.append(self._message_actions)
        self.append(header_row)

        self._reader_meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._reader_meta_box.set_hexpand(True)
        self._reader_meta_box.set_halign(Gtk.Align.FILL)
        self._reader_meta = Gtk.Label(label="", xalign=0, wrap=True)
        set_label_wrap_mode(self._reader_meta, Gtk.WrapMode.WORD_CHAR)
        self._reader_meta.add_css_class("dim-label")
        self._reader_meta.set_width_chars(1)
        self._reader_meta.set_hexpand(True)
        self._reader_meta.set_halign(Gtk.Align.FILL)
        self._reader_meta_box.append(self._reader_meta)
        self.append(self._reader_meta_box)

        self._reader_attachments = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4
        )
        self._reader_attachments.set_visible(False)
        self.append(self._reader_attachments)

        self._invite_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._invite_box.add_css_class("calendar-invite")
        self._invite_box.set_visible(False)
        self._invite_box.set_margin_top(4)
        self._invite_box.set_margin_bottom(4)
        self._invite_box.set_hexpand(True)
        self._invite_box.set_halign(Gtk.Align.FILL)
        self._invite_box.set_overflow(Gtk.Overflow.HIDDEN)

        invite_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        invite_header.set_hexpand(True)
        invite_header.set_halign(Gtk.Align.FILL)
        invite_header.set_overflow(Gtk.Overflow.HIDDEN)

        self._invite_heading = EllipsizingLabel(label="", xalign=0)
        self._invite_heading.add_css_class("heading")
        self._invite_heading.set_halign(Gtk.Align.FILL)
        self._invite_heading.set_valign(Gtk.Align.CENTER)
        invite_header.append(self._invite_heading)

        self._add_to_calendar_btn = Gtk.Button(label="Add to Calendar…")
        self._add_to_calendar_btn.set_halign(Gtk.Align.END)
        self._add_to_calendar_btn.set_valign(Gtk.Align.CENTER)
        self._add_to_calendar_btn.set_hexpand(False)
        self._add_to_calendar_btn.connect(
            "clicked", lambda *_a: self._emit_add_to_calendar()
        )
        invite_header.append(self._add_to_calendar_btn)
        self._invite_box.append(invite_header)

        self._invite_when = WrappingLabel(label="", xalign=0)
        self._invite_when.add_css_class("dim-label")
        self._invite_when.set_wrap(True)
        set_label_wrap_mode(self._invite_when, Gtk.WrapMode.WORD_CHAR)
        self._invite_when.set_hexpand(True)
        self._invite_box.append(self._invite_when)
        self._invite_where = WrappingLabel(label="", xalign=0)
        self._invite_where.add_css_class("dim-label")
        self._invite_where.set_wrap(True)
        set_label_wrap_mode(self._invite_where, Gtk.WrapMode.WORD_CHAR)
        self._invite_where.set_hexpand(True)
        self._invite_box.append(self._invite_where)
        self._invite_organizer = WrappingLabel(label="", xalign=0)
        self._invite_organizer.add_css_class("dim-label")
        self._invite_organizer.set_wrap(True)
        set_label_wrap_mode(self._invite_organizer, Gtk.WrapMode.WORD_CHAR)
        self._invite_organizer.set_hexpand(True)
        self._invite_box.append(self._invite_organizer)

        # "Link: " stays dim; only the URL is accent-colored and clickable.
        self._invite_link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._invite_link_row.set_hexpand(True)
        self._invite_link_row.set_halign(Gtk.Align.FILL)
        self._invite_link_row.set_overflow(Gtk.Overflow.HIDDEN)
        link_prefix = Gtk.Label(label="Link: ", xalign=0)
        link_prefix.add_css_class("dim-label")
        link_prefix.set_halign(Gtk.Align.START)
        link_prefix.set_hexpand(False)
        self._invite_link_row.append(link_prefix)

        self._invite_link = EllipsizingLabel(label="", xalign=0)
        self._invite_link.add_css_class("calendar-invite-link")
        self._invite_link.set_halign(Gtk.Align.FILL)
        # Selectable labels can refuse to ellipsize in GTK4; copy is via the
        # invite / link context menus instead.
        self._invite_link.set_selectable(False)
        self._invite_link.set_cursor_from_name("pointer")
        self._invite_join_url: str | None = None
        link_click = Gtk.GestureClick()
        link_click.set_button(Gdk.BUTTON_PRIMARY)
        link_click.connect("pressed", self._on_invite_link_pressed)
        self._invite_link.add_controller(link_click)
        link_menu = Gtk.GestureClick()
        link_menu.set_button(Gdk.BUTTON_SECONDARY)
        link_menu.connect("pressed", self._on_invite_link_menu_pressed)
        self._invite_link.add_controller(link_menu)
        self._invite_link_row.append(self._invite_link)
        self._invite_box.append(self._invite_link_row)

        # Bubble (not capture) so the link's own secondary-click menu runs first.
        invite_menu_gesture = Gtk.GestureClick()
        invite_menu_gesture.set_button(Gdk.BUTTON_SECONDARY)
        invite_menu_gesture.connect("pressed", self._on_invite_menu_pressed)
        self._invite_box.add_controller(invite_menu_gesture)
        self._invite_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self.append(self._invite_box)

        self._reader_body_stack = Gtk.Stack()
        self._reader_body_stack.set_vexpand(True)
        self._reader_body_stack.set_hexpand(True)

        reader_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        reader_empty_label = Gtk.Label(label="No Message Selected")
        reader_empty_label.add_css_class("dim-label")
        configure_ellipsize_label(reader_empty_label)
        reader_empty_label.set_halign(Gtk.Align.CENTER)
        reader_empty_box.append(reader_empty_label)
        self._reader_body_stack.add_named(reader_empty_box, "empty")

        self._web_view = WebKit.WebView()
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        self._web_view.connect("decide-policy", self._on_web_view_decide_policy)
        self._web_view.connect("context-menu", self._on_web_view_context_menu)
        self._web_view.connect(
            "mouse-target-changed", self._on_web_view_mouse_target_changed
        )
        self._web_view.set_vexpand(True)
        self._web_view.set_hexpand(True)
        self._web_view.set_halign(Gtk.Align.FILL)
        self._sync_web_view_background()
        # Prevent long message URLs from forcing a huge minimum width.
        self._web_view.set_size_request(1, -1)

        self._link_hover_label = Gtk.Label(label="", xalign=0)
        self._link_hover_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._link_hover_label.set_max_width_chars(80)
        self._link_hover_label.set_single_line_mode(True)
        self._link_hover_label.set_can_target(False)
        self._link_hover_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self._link_hover_box.add_css_class("osd")
        self._link_hover_box.set_can_target(False)
        self._link_hover_box.set_halign(Gtk.Align.START)
        self._link_hover_box.set_valign(Gtk.Align.START)
        self._link_hover_box.set_visible(False)
        self._link_hover_box.append(self._link_hover_label)

        body_overlay = Gtk.Overlay()
        body_overlay.set_child(self._web_view)
        body_overlay.set_vexpand(True)
        body_overlay.set_hexpand(True)
        body_overlay.set_halign(Gtk.Align.FILL)
        body_overlay.add_overlay(self._link_hover_box)
        self._link_hover_overlay = body_overlay
        self._reader_body_stack.add_named(body_overlay, "content")
        self._reader_body_stack.set_visible_child_name("empty")
        self._reader_body_stack.set_hexpand(True)
        self._reader_body_stack.set_halign(Gtk.Align.FILL)
        self._reader_body_stack.set_overflow(Gtk.Overflow.HIDDEN)

        self.append(self._reader_body_stack)

        self._setup_address_actions()

    def _setup_address_actions(self) -> None:
        group = Gio.SimpleActionGroup.new()
        new_action = Gio.SimpleAction.new("address-new-message", None)
        new_action.connect("activate", self._on_address_new_message_activate)
        group.add_action(new_action)
        search_action = Gio.SimpleAction.new("address-search-from", None)
        search_action.connect("activate", self._on_address_search_from_activate)
        group.add_action(search_action)
        copy_action = Gio.SimpleAction.new("address-copy", None)
        copy_action.connect("activate", self._on_address_copy_activate)
        group.add_action(copy_action)
        copy_invite_action = Gio.SimpleAction.new("copy-invite", None)
        copy_invite_action.connect("activate", self._on_copy_invite_activate)
        group.add_action(copy_invite_action)
        self.insert_action_group("reader", group)
        self._reader_action_group = group
        self._address_new_action = new_action
        self._address_search_action = search_action
        self._address_copy_action = copy_action
        self._copy_invite_action = copy_invite_action
        self._address_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self._invite_clipboard_text = ""
        # Popovers are not always in the action widget tree; expose the group
        # so Copy (and address actions) activate reliably.
        self._address_popover.insert_action_group("reader", group)
        self._invite_popover.insert_action_group("reader", group)

    @property
    def current_message(self) -> dict[str, Any] | None:
        return self._current_message

    def _build_message_action_buttons(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._unsubscribe_btn = self._make_message_action_button(
            "list-remove-symbolic",
            "Unsubscribe",
            self._emit_unsubscribe,
        )
        self._unsubscribe_btn.set_visible(False)
        outer.append(self._unsubscribe_btn)

        self._toolbar_add_calendar_btn = self._make_message_action_button(
            "x-office-calendar-symbolic",
            "Add to Calendar",
            self._emit_add_to_calendar,
        )
        self._toolbar_add_calendar_btn.set_visible(False)
        outer.append(self._toolbar_add_calendar_btn)

        reply_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        reply_group.add_css_class("linked")

        self._reply_btn = self._make_message_action_button(
            "mail-reply-sender-symbolic",
            "Reply",
            self._on_reply,
        )
        self._reply_all_btn = self._make_message_action_button(
            "mail-reply-all-symbolic",
            "Reply All",
            self._on_reply_all,
        )
        self._forward_btn = self._make_message_action_button(
            "mail-forward-symbolic",
            "Forward",
            self._on_forward,
        )
        reply_group.append(self._reply_btn)
        reply_group.append(self._reply_all_btn)
        reply_group.append(self._forward_btn)
        outer.append(reply_group)
        return outer

    @staticmethod
    def _make_message_action_button(
        icon_name: str, tooltip: str, handler: Callable[..., None]
    ) -> Gtk.Button:
        button = Gtk.Button()
        button.set_icon_name(icon_name)
        button.set_tooltip_text(tooltip)
        button.set_sensitive(False)
        button.connect("clicked", lambda *_args: handler())
        return button

    def set_actions_sensitive(self, sensitive: bool) -> None:
        self._unsubscribe_btn.set_sensitive(sensitive)
        self._toolbar_add_calendar_btn.set_sensitive(sensitive)
        self._add_to_calendar_btn.set_sensitive(sensitive)
        self._reply_btn.set_sensitive(sensitive)
        self._reply_all_btn.set_sensitive(sensitive)
        self._forward_btn.set_sensitive(sensitive)

    def _update_unsubscribe_button(self, msg: dict[str, Any] | None) -> None:
        has_action = bool(msg and msg.get("unsubscribe"))
        self._unsubscribe_btn.set_visible(has_action)

    def _emit_unsubscribe(self) -> None:
        msg = self._current_message
        if msg is None:
            return
        action = msg.get("unsubscribe")
        if not isinstance(action, dict):
            return
        kind = action.get("kind")
        url = action.get("url")
        if kind not in ("post", "open") or not isinstance(url, str) or not url:
            return
        self._on_unsubscribe({"kind": kind, "url": url})

    def _emit_add_to_calendar(self) -> None:
        msg = self._current_message
        if msg is None:
            return
        invite = msg.get("calendar_invite")
        if not isinstance(invite, dict):
            return
        self._on_add_to_calendar(dict(invite))

    def _on_invite_link_pressed(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        url = self._invite_join_url
        if url:
            self._on_open_uri(url)

    def _on_invite_link_activate(self, _label: Gtk.Label, uri: str) -> bool:
        if uri:
            self._on_open_uri(uri)
        return True

    def _popup_invite_context_menu(
        self,
        widget: Gtk.Widget,
        x: float,
        y: float,
        *,
        label: str,
        on_activate: Callable[[], None],
    ) -> None:
        popover = Gtk.Popover()
        popover.set_has_arrow(True)
        btn = Gtk.Button(label=label)
        btn.add_css_class("flat")
        btn.set_halign(Gtk.Align.FILL)

        def on_clicked(*_a) -> None:
            popover.popdown()
            on_activate()

        btn.connect("clicked", on_clicked)
        popover.set_child(btn)
        popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        self._invite_context_popover = popover
        popover.popup()

    def _on_invite_link_menu_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        if not self._invite_join_url:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        widget = gesture.get_widget()
        if widget is None:
            return
        self._popup_invite_context_menu(
            widget,
            x,
            y,
            label="Copy link",
            on_activate=self._on_copy_invite_link,
        )

    def _on_invite_menu_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        widget = gesture.get_widget()
        if widget is None:
            return
        if not (
            self._current_message
            and isinstance(self._current_message.get("calendar_invite"), dict)
        ):
            return
        # Prefer a direct handler popover so Copy does not depend on action
        # lookup through the popover parent chain.
        self._popup_invite_context_menu(
            widget,
            x,
            y,
            label="Copy invite",
            on_activate=self._on_copy_invite_activate,
        )

    def _on_copy_invite_link(self, *_args) -> None:
        url = self._invite_join_url
        if not url:
            return
        self._invite_clipboard_text = url
        self.get_clipboard().set(self._invite_clipboard_text)

    def _on_copy_invite_activate(self, *_args) -> None:
        from post.mail.calendar_invite import format_invite_copy_text

        msg = self._current_message
        if msg is None:
            return
        invite = msg.get("calendar_invite")
        if not isinstance(invite, dict):
            return

        plain = format_invite_copy_text(invite)
        if not plain.strip():
            return

        # Hold the string on the instance so the clipboard content cannot be GC'd
        # before paste (a common Wayland empty-paste failure mode).
        self._invite_clipboard_text = plain
        self.get_clipboard().set(self._invite_clipboard_text)

    def _clear_invite_panel(self) -> None:
        self._invite_box.set_visible(False)
        self._toolbar_add_calendar_btn.set_visible(False)
        self._invite_heading.set_label("")
        self._invite_heading.set_tooltip_text(None)
        self._invite_when.set_visible(False)
        self._invite_where.set_visible(False)
        self._invite_organizer.set_visible(False)
        self._invite_join_url = None
        self._invite_link.set_label("")
        self._invite_link_row.set_visible(False)

    def _update_invite_panel(self, msg: dict[str, Any] | None) -> None:
        from post.mail.calendar_invite import format_invite_when, invite_join_url

        invite = msg.get("calendar_invite") if msg else None
        if not isinstance(invite, dict):
            self._clear_invite_panel()
            return

        title = invite.get("title") or msg.get("subject") or "Untitled"
        heading = f"Invite: {title}"
        self._invite_heading.set_label(heading)
        self._invite_heading.set_tooltip_text(heading)

        when = format_invite_when(invite)
        if when:
            self._invite_when.set_label(f"When: {when}")
            self._invite_when.set_visible(True)
        else:
            self._invite_when.set_label("When: (set when adding to calendar)")
            self._invite_when.set_visible(True)

        join_url = invite_join_url(invite)
        location = invite.get("location")
        # Avoid duplicating a URL already shown as Link.
        if (
            isinstance(location, str)
            and location
            and (not join_url or location.rstrip("/") != join_url.rstrip("/"))
        ):
            self._invite_where.set_label(f"Where: {location}")
            self._invite_where.set_visible(True)
        else:
            self._invite_where.set_visible(False)

        organizer = invite.get("organizer")
        if organizer:
            self._invite_organizer.set_label(f"Organizer: {organizer}")
            self._invite_organizer.set_visible(True)
        else:
            self._invite_organizer.set_visible(False)

        if join_url:
            self._invite_join_url = join_url
            self._invite_link.set_use_markup(False)
            self._invite_link.set_label(join_url)
            self._invite_link.set_tooltip_text(join_url)
            self._invite_link_row.set_visible(True)
        else:
            self._invite_join_url = None
            self._invite_link_row.set_visible(False)

        self._invite_box.set_visible(True)
        # Also show the header calendar action so Add is reachable even if the
        # invite band is tight.
        self._toolbar_add_calendar_btn.set_visible(True)

    def _clear_address_rows(self) -> None:
        sibling = self._reader_meta.get_next_sibling()
        while sibling is not None:
            nxt = sibling.get_next_sibling()
            self._reader_meta_box.remove(sibling)
            sibling = nxt

    def _set_reader_meta_status(self, text: str) -> None:
        self._clear_address_rows()
        self._reader_meta.set_label(text)
        self._reader_meta.set_visible(True)

    def _show_reader_header(self, msg: dict[str, Any]) -> None:
        self._clear_address_rows()
        self._reader_meta.set_label("")
        self._reader_meta.set_visible(False)
        for row in reader_header_rows(msg):
            self._reader_meta_box.append(self._build_header_row(row))

    def _build_header_row(self, row: ReaderHeaderRow) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.set_hexpand(True)
        outer.set_halign(Gtk.Align.FILL)

        field = Gtk.Label(label=f"{row.label}:", xalign=0)
        field.add_css_class("dim-label")
        field.set_valign(Gtk.Align.START)
        outer.append(field)

        if row.addresses:
            wrap_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            wrap_box.set_hexpand(True)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            line.set_hexpand(True)
            for index, display in enumerate(row.addresses):
                if index > 0:
                    comma = Gtk.Label(label=",", xalign=0)
                    comma.add_css_class("dim-label")
                    line.append(comma)
                line.append(self._make_address_label(display))
            wrap_box.append(line)
            outer.append(wrap_box)
        else:
            value = Gtk.Label(label=row.plain or "", xalign=0, wrap=True)
            set_label_wrap_mode(value, Gtk.WrapMode.WORD_CHAR)
            value.add_css_class("dim-label")
            value.set_width_chars(1)
            value.set_hexpand(True)
            value.set_halign(Gtk.Align.FILL)
            outer.append(value)
        return outer

    def _make_address_label(self, display: str) -> Gtk.Widget:
        email = bare_email_from_address(display)
        label = Gtk.Label(label=display, xalign=0)
        label.add_css_class("dim-label")
        label.set_selectable(False)
        if not email:
            return label

        # Event box so right-click has a stable widget target.
        click_target = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        click_target.append(label)
        click_target.set_tooltip_text(email)

        menu_gesture = Gtk.GestureClick()
        menu_gesture.set_button(Gdk.BUTTON_SECONDARY)
        menu_gesture.connect("pressed", self._on_address_menu_pressed, email)
        click_target.add_controller(menu_gesture)
        return click_target

    def _on_address_menu_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        email: str,
    ) -> None:
        widget = gesture.get_widget()
        if widget is None:
            return
        self._popup_address_menu(widget, x, y, email)

    def _ensure_popover_parent(
        self, popover: Gtk.PopoverMenu, widget: Gtk.Widget
    ) -> None:
        current = popover.get_parent()
        if current is widget:
            return
        if current is not None:
            popover.popdown()
            if popover.get_parent() is current:
                popover.unparent()
        popover.set_parent(widget)

    def _sync_address_search_action(self) -> None:
        self._address_search_action.set_enabled(bool(self._can_search_messages()))

    def _popup_address_menu(
        self, widget: Gtk.Widget, x: float, y: float, email: str
    ) -> None:
        self._context_address = email
        self._sync_address_search_action()
        menu = Gio.Menu()
        menu.append(f"New Message to {email}…", "reader.address-new-message")
        menu.append(f"Search Messages from {email}", "reader.address-search-from")
        menu.append("Copy address", "reader.address-copy")
        self._address_popover.set_menu_model(menu)
        self._ensure_popover_parent(self._address_popover, widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._address_popover.set_pointing_to(rect)
        self._address_popover.popup()

    def _on_address_new_message_activate(self, *_args) -> None:
        email = self._context_address
        if not email:
            return
        self._on_new_message_to(email)

    def _on_address_search_from_activate(self, *_args) -> None:
        email = self._context_address
        if not email:
            return
        self._on_search_messages_from(email)

    def _on_address_copy_activate(self, *_args) -> None:
        email = self._context_address
        if not email:
            return
        self.get_clipboard().set(email)

    def show_loading(self) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._reader_subject.set_label("Loading message…")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status("")
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._clear_invite_panel()
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._reader_body_stack.set_visible_child_name("empty")
        self._clear_link_hover()

    def show_message(
        self,
        msg: dict[str, Any],
        *,
        body: dict[str, str | None],
        allow_remote: bool,
        dark: bool,
        message_appearance: MessageAppearance = MESSAGE_APPEARANCE_ADAPT_TEXT,
        show_actions: bool = True,
    ) -> None:
        self._current_message = msg
        self._current_body = body
        self._allow_remote = allow_remote
        self._dark = dark
        self._message_appearance = message_appearance
        self._reader_subject.set_label(msg.get("subject") or "(no subject)")
        self._reader_subject.set_visible(True)
        self._show_reader_header(msg)
        self._show_attachments(
            msg.get("attachments") or [],
            hide_calendar=show_actions and isinstance(msg.get("calendar_invite"), dict),
        )
        if show_actions:
            self._update_unsubscribe_button(msg)
            self._update_invite_panel(msg)
            self._message_actions.set_visible(True)
            self.set_actions_sensitive(True)
        else:
            self._update_unsubscribe_button(None)
            self._clear_invite_panel()
            self._message_actions.set_visible(False)
            self.set_actions_sensitive(False)
        self._show_reader_document()

    def clear(self) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._reader_subject.set_label("")
        self._reader_subject.set_visible(False)
        self._set_reader_meta_status("")
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._clear_invite_panel()
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._reader_body_stack.set_visible_child_name("empty")
        self._clear_link_hover()

    def show_unavailable(self, message: str, *, dark: bool) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._dark = dark
        self._reader_subject.set_label("Message unavailable")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status(message)
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._clear_invite_panel()
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._clear_link_hover()
        self._reader_body_stack.set_visible_child_name("empty")

    def show_error(self, error: Exception, *, dark: bool) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._dark = dark
        self._reader_subject.set_label("Could not read message")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status(str(error))
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._clear_invite_panel()
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._load_error_html("This message could not be loaded.")

    def update_message_flags(self, flags: dict[str, Any]) -> None:
        if self._current_message is None:
            return
        current_flags = dict(self._current_message.get("flags") or {})
        current_flags.update(flags)
        self._current_message["flags"] = current_flags

    def refresh_document(
        self,
        *,
        allow_remote: bool | None = None,
        dark: bool | None = None,
        message_appearance: MessageAppearance | None = None,
    ) -> None:
        if allow_remote is not None:
            self._allow_remote = allow_remote
        if dark is not None:
            self._dark = dark
        if message_appearance is not None:
            self._message_appearance = message_appearance
        self._sync_web_view_background()
        if self._current_message is not None:
            self._show_reader_document()

    def _sync_web_view_background(self) -> None:
        """Match the GTK WebView canvas to the reader shell (not UA white)."""
        reader_dark = self._dark
        if self._message_appearance == MESSAGE_APPEARANCE_ADAPT_BACKGROUND:
            reader_dark = not self._dark
        color = "#1e1e1e" if reader_dark else "#ffffff"
        rgba = Gdk.RGBA()
        rgba.parse(color)
        setter = getattr(self._web_view, "set_background_color", None)
        if setter is not None:
            setter(rgba)

    def _load_error_html(self, message: str) -> None:
        self._clear_link_hover()
        self._reader_body_stack.set_visible_child_name("content")
        self._sync_web_view_background()
        error_color = "#aaaaaa" if self._dark else "#666666"
        self._web_view.load_html(
            "<body style='font-family:sans-serif;"
            f"color:{error_color};padding:1em'>"
            f"{message}</body>",
            None,
        )

    def _clear_attachments(self) -> None:
        while child := self._reader_attachments.get_first_child():
            self._reader_attachments.remove(child)
        self._reader_attachments.set_visible(False)

    def _show_attachments(
        self,
        attachments: list[dict[str, Any]],
        *,
        hide_calendar: bool = False,
    ) -> None:
        from post.mail.calendar_invite import looks_like_calendar_attachment

        self._clear_attachments()
        visible = []
        for attachment in attachments:
            mime_type = attachment.get("mime_type")
            filename = attachment.get("filename")
            if hide_calendar and looks_like_calendar_attachment(
                mime_type if isinstance(mime_type, str) else None,
                filename if isinstance(filename, str) else None,
            ):
                continue
            visible.append(attachment)
        if not visible:
            return

        heading = Gtk.Label(label="Attachments", xalign=0)
        heading.add_css_class("heading")
        self._reader_attachments.append(heading)

        list_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        list_column.set_hexpand(True)
        list_column.set_halign(Gtk.Align.FILL)

        for attachment in visible:
            index = attachment.get("index", 0)
            name = attachment.get("filename") or "attachment"
            mime_type = attachment.get("mime_type")
            size = format_attachment_size(attachment.get("size"))
            label_text = f"{name} ({size})" if size else name

            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open Attachment")
            btn.set_hexpand(True)
            btn.set_halign(Gtk.Align.FILL)
            btn.connect("clicked", self._on_attachment_button_clicked, index)

            menu_gesture = Gtk.GestureClick()
            menu_gesture.set_button(Gdk.BUTTON_SECONDARY)
            menu_gesture.connect(
                "pressed",
                self._on_attachment_menu_pressed,
                index,
                mime_type,
                name,
            )
            btn.add_controller(menu_gesture)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
            icon.add_css_class("dim-label")
            label = Gtk.Label(label=label_text, xalign=0, ellipsize=3)
            configure_ellipsize_label(label)
            label.set_halign(Gtk.Align.FILL)
            row.append(icon)
            row.append(label)
            btn.set_child(row)
            list_column.append(btn)

        self._reader_attachments.append(list_column)
        self._reader_attachments.set_visible(True)

    def _on_attachment_button_clicked(self, _button: Gtk.Button, index: int) -> None:
        self._on_attachment_clicked(index)

    def _on_attachment_menu_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        index: int,
        mime_type: str | None,
        name: str,
    ) -> None:
        widget = gesture.get_widget()
        if widget is None:
            return
        self._on_attachment_context_menu(widget, x, y, index, mime_type, name)

    def _show_reader_document(self) -> None:
        self._clear_link_hover()
        if self._current_message is None:
            self._reader_body_stack.set_visible_child_name("empty")
            return

        self._reader_body_stack.set_visible_child_name("content")
        self._sync_web_view_background()
        document = build_reader_document(
            body_html=self._current_body.get("html"),
            body_plain=self._current_body.get("plain"),
            allow_remote=self._allow_remote,
            dark=self._dark,
            message_appearance=self._message_appearance,
            inline_images=self._current_message.get("inline_images"),
        )
        self._web_view.load_html(document, None)

    @staticmethod
    def _uri_opens_externally(uri: str) -> bool:
        lower = uri.lower()
        return lower.startswith(("http://", "https://", "mailto:"))

    def _clear_link_hover(self) -> None:
        apply_reader_link_hover(self._link_hover_box, self._link_hover_label, None)

    def _position_link_hover(
        self, pointer: tuple[float, float] | None = None
    ) -> None:
        if not self._link_hover_box.get_visible():
            return
        overlay = self._link_hover_overlay
        if pointer is None:
            pointer = pointer_coords_in_widget(overlay)
        if pointer is None:
            return
        _min_w, chip_w, _, _ = self._link_hover_box.measure(
            Gtk.Orientation.HORIZONTAL, -1
        )
        _min_h, chip_h, _, _ = self._link_hover_box.measure(
            Gtk.Orientation.VERTICAL, -1
        )
        x, y = reader_link_hover_origin(
            pointer[0],
            pointer[1],
            chip_w,
            chip_h,
            overlay.get_width(),
            overlay.get_height(),
        )
        self._link_hover_box.set_margin_start(max(0, int(round(x))))
        self._link_hover_box.set_margin_top(max(0, int(round(y))))

    def _on_web_view_mouse_target_changed(
        self,
        _web_view: WebKit.WebView,
        hit_test_result: WebKit.HitTestResult,
        _modifiers: int,
    ) -> None:
        if apply_reader_link_hover(
            self._link_hover_box,
            self._link_hover_label,
            reader_link_tooltip_text(hit_test_result),
        ):
            self._position_link_hover()

    def _on_web_view_context_menu(
        self,
        _web_view: WebKit.WebView,
        context_menu: WebKit.ContextMenu,
        hit_test_result: WebKit.HitTestResult,
    ) -> bool:
        strip_reader_context_menu(context_menu)
        email = ""
        if hit_test_result is not None and hit_test_result.context_is_link():
            uri = hit_test_result.get_link_uri() or ""
            email = mailto_primary_email(uri)
        if email:
            self._context_address = email
            self._sync_address_search_action()
            prepend_address_context_menu_items(
                context_menu,
                new_message_action=self._address_new_action,
                search_from_action=self._address_search_action,
                copy_address_action=self._address_copy_action,
                email=email,
            )
        return False

    def _on_web_view_decide_policy(
        self,
        _web_view: WebKit.WebView,
        decision: WebKit.NavigationPolicyDecision,
        decision_type: WebKit.PolicyDecisionType,
    ) -> bool:
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False

        navigation = decision.get_navigation_action()
        if navigation is None:
            return False

        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav_type = navigation.get_navigation_type()
            if nav_type not in (
                WebKit.NavigationType.LINK_CLICKED,
                WebKit.NavigationType.FORM_SUBMITTED,
                WebKit.NavigationType.FORM_RESUBMITTED,
            ):
                return False

        request = navigation.get_request()
        if request is None:
            return False

        uri = request.get_uri()
        if not uri or not self._uri_opens_externally(uri):
            decision.ignore()
            return True

        self._on_open_uri(uri)
        decision.ignore()
        return True
