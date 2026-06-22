# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Adw.Application entry."""

from __future__ import annotations

import logging
import os
import sys
import warnings

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from post.icon_utils import APP_ICON_NAME, register_bundled_icons
from post.window import MainWindow


def main() -> int:
    # POST_LOG_LEVEL=DEBUG enables verbose send-phase logging in post.mail.eds.
    log_level = os.environ.get("POST_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
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
