# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Schedule work on the GTK main loop from other threads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

_gtk_main_context = GLib.MainContext.default()


def schedule_on_gtk_main(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """Run ``func`` on the GTK main loop (safe from mail I/O or worker threads)."""

    def wrapper() -> bool:
        func(*args, **kwargs)
        return False

    _gtk_main_context.invoke_full(GLib.PRIORITY_DEFAULT, wrapper)
