# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared Adw.HeaderBar helpers."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")

from gi.repository import Adw

_END_DECORATION_LAYOUT = ":minimize,maximize,close"


def add_end_window_controls(header: Adw.HeaderBar) -> None:
    """Show minimize, maximize, and close on the header's right edge."""
    header.set_show_end_title_buttons(True)
    header.set_decoration_layout(_END_DECORATION_LAYOUT)
