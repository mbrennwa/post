# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared Adw.HeaderBar helpers."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gdk, Gtk

_END_DECORATION_LAYOUT = ":minimize,maximize,close"

# Adwaita already insets header contents by 6px; this adds another 4px on each
# side so start actions and end title buttons share equal ~10px corner gaps
# (enough to clear typical window corner radii).
_HEADER_CORNER_INSET_CSS = """
headerbar.post-header-corner-inset {
  padding: 4px;
}
"""
_corner_inset_provider: Gtk.CssProvider | None = None


def add_end_window_controls(header: Adw.HeaderBar) -> None:
    """Show minimize, maximize, and close on the header's right edge."""
    header.set_show_end_title_buttons(True)
    header.set_decoration_layout(_END_DECORATION_LAYOUT)


def apply_header_corner_inset(header: Adw.HeaderBar) -> None:
    """Give start actions and end title buttons matching top/side corner gaps."""
    global _corner_inset_provider
    header.add_css_class("post-header-corner-inset")
    if _corner_inset_provider is not None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(_HEADER_CORNER_INSET_CSS)
    display = Gdk.Display.get_default()
    if display is None:
        return
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _corner_inset_provider = provider
