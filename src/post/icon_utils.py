# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Application icon helpers."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk

APP_ICON_NAME = "io.github.mbrennwa.Post"


def bundled_icons_root() -> Path:
    return Path(__file__).resolve().parent / "icons"


def register_bundled_icons() -> None:
    root = bundled_icons_root()
    if not root.is_dir():
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    Gtk.IconTheme.get_for_display(display).add_search_path(str(root))


def apply_window_icon(window: Gtk.Window) -> None:
    window.set_icon_name(APP_ICON_NAME)
