# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Open an attachment in Post (attached email) or via the desktop."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk

from post.mail.helpers import looks_like_rfc822_attachment, write_temp_attachment
from post.toast import show_error_toast

OnAddressEmail = Callable[[str], None]
CanSearchMessages = Callable[[], bool]
OnStatus = Callable[[str], None]


def open_attachment(
    parent: Gtk.Window,
    *,
    filename: str,
    data: bytes,
    mime_type: str | None = None,
    on_new_message_to: OnAddressEmail | None = None,
    on_search_messages_from: OnAddressEmail | None = None,
    can_search_messages: CanSearchMessages | None = None,
    on_status: OnStatus | None = None,
) -> None:
    """Show an attached email in Post, or launch the desktop handler."""
    if looks_like_rfc822_attachment(mime_type, filename, data):
        from post.attached_message_window import present_attached_message

        present_attached_message(
            parent,
            filename=filename,
            data=data,
            on_new_message_to=on_new_message_to,
            on_search_messages_from=on_search_messages_from,
            can_search_messages=can_search_messages,
            on_status=on_status,
        )
        return

    try:
        path = write_temp_attachment(filename, data)
        file = Gio.File.new_for_path(path)
        Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
    except (OSError, GLib.Error) as exc:
        message = getattr(exc, "message", None) or str(exc)
        show_error_toast(parent, f"Could not open attachment: {message}")
        return
    if on_status is not None:
        on_status(f"Opened {filename}")


def launch_attachment_with_app(
    parent: Gtk.Window,
    *,
    filename: str,
    data: bytes,
    mime_type: str | None = None,
    on_status: OnStatus | None = None,
) -> None:
    """Write a temp file and show the desktop Open With dialog."""
    try:
        path = write_temp_attachment(filename, data)
    except OSError as exc:
        show_error_toast(parent, f"Could not open attachment: {exc}")
        return

    content_type = mime_type or "application/octet-stream"
    if not mime_type:
        guessed, _certain = Gio.content_type_guess(filename, data)
        content_type = guessed or content_type

    dialog = Gtk.AppChooserDialog.new_for_content_type(
        parent,
        Gtk.DialogFlags.MODAL,
        content_type,
    )
    dialog.set_heading("Open With")

    def on_response(_dialog: Gtk.AppChooserDialog, response: int) -> None:
        if response == Gtk.ResponseType.OK:
            app_info = _dialog.get_app_info()
            if app_info is not None:
                file = Gio.File.new_for_path(path)
                try:
                    app_info.launch_uris([file.get_uri()], None)
                    if on_status is not None:
                        on_status(f"Opened {filename}")
                except GLib.Error as exc:
                    show_error_toast(
                        parent, f"Could not open attachment: {exc.message}"
                    )
        _dialog.destroy()

    dialog.connect("response", on_response)
    dialog.present()
