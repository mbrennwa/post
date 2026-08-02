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

from gi.repository import Gdk, Gio, Gtk, WebKit

from post.mail.helpers import (
    ReaderHeaderRow,
    bare_email_from_address,
    format_attachment_size,
    mailto_primary_email,
    reader_header_rows,
)
from post.preferences import (
    MESSAGE_APPEARANCE_ADAPT_TEXT,
    MessageAppearance,
    get_load_remote_content,
    get_message_appearance,
)
from post.reader.html import build_reader_document
from post.wrap_label import WrappingLabel, configure_ellipsize_label, set_label_wrap_mode

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


def prepend_address_context_menu_items(
    menu: WebKit.ContextMenu,
    *,
    new_message_action: Gio.Action,
    search_from_action: Gio.Action,
    email: str,
) -> None:
    """Prepend New Message / Search Messages actions for *email*."""
    had_items = bool(list(menu.get_items()))
    if had_items:
        menu.prepend(WebKit.ContextMenuItem.new_separator())
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
    """BoxLayout that never reports natural size below minimum.

    Under tight height-for-width passes, Gtk.BoxLayout can compute
    natural < minimum for MessageReaderPane (wrapping header + WebKit).
    """

    __gtype_name__ = "PostClampingBoxLayout"

    def do_measure(
        self, widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
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
        self._on_reply = on_reply
        self._on_reply_all = on_reply_all
        self._on_forward = on_forward
        self._on_unsubscribe = on_unsubscribe
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
        self._web_view.set_vexpand(True)
        self._web_view.set_hexpand(True)
        self._reader_body_stack.add_named(self._web_view, "content")
        self._reader_body_stack.set_visible_child_name("empty")

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
        self.insert_action_group("reader", group)
        self._address_new_action = new_action
        self._address_search_action = search_action
        self._address_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())

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

    def show_loading(self) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._reader_subject.set_label("Loading message…")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status("")
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._reader_body_stack.set_visible_child_name("empty")

    def show_message(
        self,
        msg: dict[str, Any],
        *,
        body: dict[str, str | None],
        allow_remote: bool,
        dark: bool,
        message_appearance: MessageAppearance = MESSAGE_APPEARANCE_ADAPT_TEXT,
    ) -> None:
        self._current_message = msg
        self._current_body = body
        self._allow_remote = allow_remote
        self._dark = dark
        self._message_appearance = message_appearance
        self._reader_subject.set_label(msg.get("subject") or "(no subject)")
        self._reader_subject.set_visible(True)
        self._show_reader_header(msg)
        self._show_attachments(msg.get("attachments") or [])
        self._update_unsubscribe_button(msg)
        self._message_actions.set_visible(True)
        self.set_actions_sensitive(True)
        self._show_reader_document()

    def clear(self) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._reader_subject.set_label("")
        self._reader_subject.set_visible(False)
        self._set_reader_meta_status("")
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._reader_body_stack.set_visible_child_name("empty")

    def show_unavailable(self, message: str, *, dark: bool) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._dark = dark
        self._reader_subject.set_label("Message unavailable")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status(message)
        self._clear_attachments()
        self._update_unsubscribe_button(None)
        self._message_actions.set_visible(False)
        self.set_actions_sensitive(False)
        self._load_error_html(message)

    def show_error(self, error: Exception, *, dark: bool) -> None:
        self._current_message = None
        self._current_body = {"plain": None, "html": None}
        self._dark = dark
        self._reader_subject.set_label("Could not read message")
        self._reader_subject.set_visible(True)
        self._set_reader_meta_status(str(error))
        self._clear_attachments()
        self._update_unsubscribe_button(None)
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
        if self._current_message is not None:
            self._show_reader_document()

    def _load_error_html(self, message: str) -> None:
        self._reader_body_stack.set_visible_child_name("content")
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

    def _show_attachments(self, attachments: list[dict[str, Any]]) -> None:
        self._clear_attachments()
        if not attachments:
            return

        heading = Gtk.Label(label="Attachments", xalign=0)
        heading.add_css_class("heading")
        self._reader_attachments.append(heading)

        list_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        list_column.set_hexpand(True)
        list_column.set_halign(Gtk.Align.FILL)

        for attachment in attachments:
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
        if self._current_message is None:
            self._reader_body_stack.set_visible_child_name("empty")
            return

        self._reader_body_stack.set_visible_child_name("content")
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
