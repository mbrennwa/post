# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared Adw.Toast notifications."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk

ToastParent = Gtk.Window | Adw.ApplicationWindow | Adw.Window | Adw.PreferencesDialog


def show_toast(
    parent: ToastParent,
    message: str,
    *,
    priority: Adw.ToastPriority = Adw.ToastPriority.NORMAL,
    timeout: int = 5,
) -> None:
    """Show a non-blocking toast on the given window."""
    if not message:
        return

    overlay = _toast_overlay_for(parent)
    toast = Adw.Toast.new(message)
    toast.set_priority(priority)
    if timeout > 0:
        toast.set_timeout(timeout)
    overlay.add_toast(toast)


def show_error_toast(
    parent: ToastParent,
    message: str,
    *,
    heading: str | None = None,
    timeout: int = 8,
) -> None:
    """Show a high-priority toast for an error or warning."""
    text = message if not heading else f"{heading}: {message}"
    show_toast(parent, text, priority=Adw.ToastPriority.HIGH, timeout=timeout)


def _toast_host_window(parent: ToastParent) -> Gtk.Window | Adw.ApplicationWindow | Adw.Window:
    if isinstance(parent, Adw.PreferencesDialog):
        host = parent.get_transient_for()
        if host is not None:
            return host
    return parent


def _toast_overlay_for(parent: ToastParent) -> Adw.ToastOverlay:
    window = _toast_host_window(parent)

    existing = getattr(window, "_toast_overlay", None)
    if isinstance(existing, Adw.ToastOverlay):
        return existing

    content = window.get_content()
    if isinstance(content, Adw.ToastOverlay):
        window._toast_overlay = content
        return content

    if content is None:
        raise TypeError(f"Cannot show toast for {type(window)!r}")

    overlay = Adw.ToastOverlay()
    overlay.set_child(content)
    overlay.set_vexpand(True)
    overlay.set_hexpand(True)
    window.set_content(overlay)
    window._toast_overlay = overlay
    return overlay
