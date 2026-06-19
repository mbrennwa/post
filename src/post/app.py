# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Adw.Application entry."""

from __future__ import annotations

import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib

from post.window import MainWindow

log = logging.getLogger(__name__)


def _debug_enabled() -> bool:
    return os.environ.get("POST_DEBUG", "").lower() not in ("", "0", "false", "no")


def configure_logging() -> None:
    level = logging.DEBUG if _debug_enabled() else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    if _debug_enabled():
        log.info("Post debug logging enabled (POST_DEBUG or --debug)")


def main() -> int:
    if "--debug" in sys.argv:
        sys.argv.remove("--debug")
        os.environ["POST_DEBUG"] = "1"

    configure_logging()

    app = Adw.Application(application_id="io.github.mbrennwa.Post")

    def on_activate(application: Adw.Application) -> None:
        win = application.get_active_window()
        if win is None:
            win = MainWindow(application=application)
            win.present()
            win.begin_load()
        else:
            win.present()

    app.connect("activate", on_activate)
    return app.run(sys.argv)
