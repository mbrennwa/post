# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only window for an attached RFC 822 message (#385)."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from post.header_bar import add_end_window_controls
from post.icon_utils import apply_window_icon
from post.mail.helpers import (
    get_attachment_data_from_rfc822_bytes,
    message_dict_from_rfc822_bytes,
)
from post.open_uri import open_uri_externally
from post.preferences import get_load_remote_content, get_message_appearance
from post.reader.pane import MessageReaderPane
from post.toast import show_error_toast, show_toast

log = logging.getLogger(__name__)

OnAddressEmail = Callable[[str], None]
CanSearchMessages = Callable[[], bool]
OnStatus = Callable[[str], None]

_open_windows: dict[str, AttachedMessageWindow] = {}


def _payload_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def present_attached_message(
    parent: Gtk.Window,
    *,
    filename: str,
    data: bytes,
    on_new_message_to: OnAddressEmail | None = None,
    on_search_messages_from: OnAddressEmail | None = None,
    can_search_messages: CanSearchMessages | None = None,
    on_status: OnStatus | None = None,
) -> AttachedMessageWindow:
    """Open or raise the nested reader for *data*."""
    key = _payload_key(data)
    existing = _open_windows.get(key)
    if existing is not None:
        existing.present()
        return existing
    window = AttachedMessageWindow(
        parent=parent,
        filename=filename,
        data=data,
        window_key=key,
        on_new_message_to=on_new_message_to,
        on_search_messages_from=on_search_messages_from,
        can_search_messages=can_search_messages,
        on_status=on_status,
    )
    _open_windows[key] = window
    window.connect("destroy", lambda *_args: _open_windows.pop(key, None))
    window.present()
    return window


class AttachedMessageWindow(Adw.ApplicationWindow):
    """Read-only viewer for a ``message/rfc822`` attachment."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        filename: str,
        data: bytes,
        window_key: str,
        on_new_message_to: OnAddressEmail | None,
        on_search_messages_from: OnAddressEmail | None,
        can_search_messages: CanSearchMessages | None,
        on_status: OnStatus | None,
    ) -> None:
        super().__init__()
        apply_window_icon(self)
        application = parent.get_application()
        if application is not None:
            self.set_application(application)

        self._raw = data
        self._window_key = window_key
        self._on_new_message_to = on_new_message_to
        self._on_search_messages_from = on_search_messages_from
        self._can_search_messages = can_search_messages
        self._on_status = on_status
        self._context_attachment_index: int | None = None
        self._context_attachment_mime: str | None = None
        self._context_attachment_name: str | None = None

        try:
            msg = message_dict_from_rfc822_bytes(data)
        except Exception:
            log.exception("Failed to parse attached message")
            msg = {
                "subject": filename or "(no subject)",
                "from": "",
                "to": "",
                "attachments": [],
                "inline_images": {},
                "flags": {},
                "body_plain": None,
                "body_html": None,
            }
        self._current_message: dict[str, Any] = msg
        subject = msg.get("subject") or "(no subject)"
        self.set_title(subject)
        self.set_default_size(720, 560)

        header = Adw.HeaderBar()
        add_end_window_controls(header)

        self._reader_pane = MessageReaderPane(
            on_reply=self._noop,
            on_reply_all=self._noop,
            on_forward=self._noop,
            on_unsubscribe=self._noop,
            on_add_to_calendar=self._noop,
            on_attachment_clicked=self._on_attachment_clicked,
            on_attachment_context_menu=self._on_attachment_context_menu,
            on_open_uri=self._open_uri_externally,
            on_new_message_to=self._emit_new_message_to,
            on_search_messages_from=self._emit_search_from,
            can_search_messages=self._can_search,
        )
        self._reader_pane.set_margin_start(16)
        self._reader_pane.set_margin_end(16)
        self._reader_pane.set_margin_top(12)
        self._reader_pane.set_margin_bottom(12)
        self._reader_pane.set_vexpand(True)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._reader_pane)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)
        self._toast_overlay.set_child(toolbar_view)
        self.set_content(self._toast_overlay)

        self._setup_attachment_menu()

        dark = Adw.StyleManager.get_default().get_dark()
        self._reader_pane.show_message(
            msg,
            body={
                "plain": msg.get("body_plain"),
                "html": msg.get("body_html"),
            },
            allow_remote=get_load_remote_content(),
            dark=dark,
            message_appearance=get_message_appearance(),
            show_actions=False,
        )

        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._on_app_dark_changed)

    @staticmethod
    def _noop(*_args: Any) -> None:
        return None

    def _can_search(self) -> bool:
        if self._can_search_messages is None:
            return False
        return bool(self._can_search_messages())

    def _emit_new_message_to(self, email: str) -> None:
        if self._on_new_message_to is not None:
            self._on_new_message_to(email)

    def _emit_search_from(self, email: str) -> None:
        if self._on_search_messages_from is not None:
            self._on_search_messages_from(email)

    def _on_app_dark_changed(self, *_args: Any) -> None:
        self._reader_pane.refresh_document(
            dark=Adw.StyleManager.get_default().get_dark()
        )

    def _open_uri_externally(self, uri: str) -> None:
        open_uri_externally(
            self,
            uri,
            on_error=lambda message: show_error_toast(self, message),
        )

    def _setup_attachment_menu(self) -> None:
        save_action = Gio.SimpleAction.new("save-attachment", None)
        save_action.connect("activate", self._on_attachment_menu_save)
        self.add_action(save_action)
        open_with_action = Gio.SimpleAction.new("open-with-attachment", None)
        open_with_action.connect("activate", self._on_attachment_menu_open_with)
        self.add_action(open_with_action)
        menu = Gio.Menu()
        menu.append("Save As…", "win.save-attachment")
        menu.append("Open With…", "win.open-with-attachment")
        self._attachment_popover = Gtk.PopoverMenu()
        self._attachment_popover.set_menu_model(menu)

    def _on_attachment_context_menu(
        self,
        widget: Gtk.Widget,
        x: float,
        y: float,
        index: int,
        mime_type: str | None,
        name: str,
    ) -> None:
        self._context_attachment_index = index
        self._context_attachment_mime = mime_type
        self._context_attachment_name = name
        self._attachment_popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._attachment_popover.set_pointing_to(rect)
        self._attachment_popover.popup()

    def _on_attachment_clicked(self, attachment_index: int) -> None:
        self._open_inner_attachment(attachment_index, open_with=False)

    def _on_attachment_menu_save(self, *_args: Any) -> None:
        if self._context_attachment_index is None:
            return
        try:
            filename, data = get_attachment_data_from_rfc822_bytes(
                self._raw, self._context_attachment_index
            )
        except Exception as exc:
            show_error_toast(self, f"Could not save attachment: {exc}")
            return
        if self._context_attachment_name:
            filename = self._context_attachment_name
        self._prompt_save_attachment(filename, data)

    def _on_attachment_menu_open_with(self, *_args: Any) -> None:
        if self._context_attachment_index is None:
            return
        self._open_inner_attachment(self._context_attachment_index, open_with=True)

    def _open_inner_attachment(self, index: int, *, open_with: bool) -> None:
        from post.attachment_open import launch_attachment_with_app, open_attachment

        try:
            filename, data = get_attachment_data_from_rfc822_bytes(self._raw, index)
        except Exception as exc:
            log.exception("Failed to read nested attachment")
            show_error_toast(self, f"Could not open attachment: {exc}")
            return
        mime = self._mime_for_index(index)
        if open_with:
            launch_attachment_with_app(
                self,
                filename=filename,
                data=data,
                mime_type=mime,
                on_status=self._on_status,
            )
            return
        open_attachment(
            self,
            filename=filename,
            data=data,
            mime_type=mime,
            on_new_message_to=self._on_new_message_to,
            on_search_messages_from=self._on_search_messages_from,
            can_search_messages=self._can_search_messages,
            on_status=self._on_status,
        )

    def _mime_for_index(self, index: int) -> str | None:
        attachments = self._current_message.get("attachments") or []
        if 0 <= index < len(attachments):
            mime = attachments[index].get("mime_type")
            return mime if isinstance(mime, str) else None
        return self._context_attachment_mime

    def _prompt_save_attachment(self, filename: str, data: bytes) -> None:
        dialog = Gtk.FileDialog(title="Save Attachment")
        dialog.set_initial_name(filename)

        def on_response(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = _dialog.save_finish(result)
            except GLib.Error as exc:
                if exc.matches(Gtk.dialog_error_quark(), int(Gtk.DialogError.DISMISSED)):
                    return
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    return
                show_error_toast(self, f"Could not save attachment: {exc.message}")
                return
            path = file.get_path()
            if path is None:
                return
            try:
                with open(path, "wb") as handle:
                    handle.write(data)
                show_toast(self, f"Saved {os.path.basename(path)}")
            except OSError as exc:
                show_error_toast(self, f"Could not save attachment: {exc}")

        dialog.save(self, None, on_response)
