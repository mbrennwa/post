# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Adw.Application entry."""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw

from post.window import MainWindow

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    app = Adw.Application(application_id="io.github.mbrennwa.Post")

    def on_activate(application: Adw.Application) -> None:
        win = application.get_active_window()
        if win is None:
            win = MainWindow(application=application)
        win.present()

    app.connect("activate", on_activate)
    return app.run(sys.argv)
