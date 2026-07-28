# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Adw.Application entry."""

from __future__ import annotations

import sys
import warnings

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from post.icon_utils import APP_ICON_NAME, register_bundled_icons
from post.window import MainWindow


def main() -> int:
    # Always-on rotating file log under XDG state (~/.local/state/post/post.log).
    # POST_LOG_LEVEL=DEBUG enables mail I/O task tracing (io_thread, eds send path)
    # and raises file + stderr verbosity.
    # POST_DEBUG_SEARCH=1 enables folder search tracing (post.search logger).
    from post.logging_setup import configure_logging
    from post.mail.search_debug import configure_search_debug_logging

    configure_logging()
    configure_search_debug_logging()
    # PyGObject adds an extra ref when Python vfuncs return GObjects; harmless at exit.
    warnings.filterwarnings(
        "ignore",
        message=r"Adding extra reference for.*FilterDriver",
        category=RuntimeWarning,
    )

    app = Adw.Application(application_id=APP_ICON_NAME)

    def on_startup(_application: Adw.Application) -> None:
        register_bundled_icons()
        Gtk.Window.set_default_icon_name(APP_ICON_NAME)
        from post.mail.io_thread import get_mail_io_thread

        get_mail_io_thread()

    def on_activate(application: Adw.Application) -> None:
        win = application.get_active_window()
        if win is None:
            win = MainWindow(application=application)
            win.present()
            win.begin_load()
        else:
            win.present()

    app.connect("startup", on_startup)
    app.connect("activate", on_activate)
    return app.run(sys.argv)
