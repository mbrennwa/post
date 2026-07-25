# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Open http(s)/mailto URIs with the desktop default handler."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk

OnUriError = Callable[[str], None]


def _app_launch_context(parent: Gtk.Window) -> Gdk.AppLaunchContext | None:
    display = parent.get_display() or Gdk.Display.get_default()
    if display is None:
        return None
    context = display.get_app_launch_context()
    # Timestamp helps focus-stealing prevention so the browser can raise.
    time = Gtk.get_current_event_time() if hasattr(Gtk, "get_current_event_time") else 0
    if not time:
        surface = parent.get_surface()
        if surface is not None and hasattr(surface, "get_current_event_time"):
            time = surface.get_current_event_time()
    if time:
        context.set_timestamp(time)
    return context


def open_uri_externally(
    parent: Gtk.Window,
    uri: str,
    *,
    on_error: OnUriError | None = None,
) -> None:
    """Launch *uri*, preferring Gtk.UriLauncher so the parent supplies activation."""

    def report_error(message: str) -> None:
        if on_error is not None:
            on_error(message)

    if hasattr(Gtk, "UriLauncher"):
        launcher = Gtk.UriLauncher.new(uri)

        def on_finished(_launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
            try:
                launcher.launch_finish(result)
            except GLib.Error as exc:
                report_error(f"Could not open link: {exc.message}")

        # Pass the window as parent so Wayland/X11 can attach an activation token
        # from the current user gesture and raise the browser.
        launcher.launch(parent, None, on_finished)
        return

    try:
        context = _app_launch_context(parent)
        if not Gio.AppInfo.launch_default_for_uri(uri, context):
            report_error("Could not open link")
    except GLib.Error as exc:
        report_error(f"Could not open link: {exc.message}")
