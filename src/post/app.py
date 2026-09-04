# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Adw.Application entry."""

from __future__ import annotations

import logging
import sys
import warnings

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, Gio, GLib, Gtk

from post.icon_utils import APP_ICON_NAME, register_bundled_icons
from post.window import MainWindow

log = logging.getLogger("post.app")


def _destroy_half_built_main_windows(application: Adw.Application) -> None:
    """Drop MainWindows left registered if ``__init__`` raised mid-construction."""
    for win in list(application.get_windows()):
        if isinstance(win, MainWindow):
            win.destroy()


def _show_startup_failure(application: Adw.Application, message: str) -> None:
    """Standalone modal alert (no parent window), then return (caller quits).

    ``Adw.AlertDialog.present(None)`` shows as its own window when there is no
    host (#390). Avoid a throwaway ApplicationWindow — it peeks out behind the
    dialog.
    """
    loop = GLib.MainLoop()
    dialog = Adw.AlertDialog(heading="Cannot start Post", body=message)
    dialog.add_response("quit", "Quit")
    dialog.set_default_response("quit")
    dialog.set_close_response("quit")

    def on_response(_dialog: Adw.AlertDialog, _response: str) -> None:
        loop.quit()

    dialog.connect("response", on_response)
    # Keep the Gio.Application alive while the nested loop runs.
    application.hold()
    try:
        dialog.present(None)
        loop.run()
    finally:
        application.release()


def _ensure_main_window(application: Adw.Application) -> MainWindow | None:
    for win in application.get_windows():
        if isinstance(win, MainWindow):
            win.present()
            return win
    try:
        win = MainWindow(application=application)
    except RuntimeError as exc:
        log.error("%s", exc)
        _destroy_half_built_main_windows(application)
        _show_startup_failure(application, str(exc))
        application.quit()
        return None
    win.present()
    win.begin_load()
    return win


class PostApplication(Adw.Application):
    """Single-instance app that accepts mailto: on the command line."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ICON_NAME,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.connect("startup", self._on_startup)
        self.connect("activate", self._on_activate)

    def _on_startup(self, *_args) -> None:
        register_bundled_icons()
        Gtk.Window.set_default_icon_name(APP_ICON_NAME)
        self._install_standard_shortcuts()
        from post.mail.io_thread import get_mail_io_thread

        get_mail_io_thread()
        from post.spell_check import ensure_spell_check_initialized

        ensure_spell_check_initialized()

    def _install_standard_shortcuts(self) -> None:
        """GNOME HIG: Ctrl+W closes the focused window; Ctrl+Q quits the app."""
        # GTK's built-in close action is "window.close" (not "win.close").
        # "win.*" is only for actions added via Gtk.ApplicationWindow.add_action().
        self.set_accels_for_action("window.close", ["<Control>w"])
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit_requested)
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_quit_requested(self, *_args) -> None:
        # Route through MainWindow.close() so pending send/move/delayed-send
        # gates run. Do not call application.quit() directly.
        for win in self.get_windows():
            if isinstance(win, MainWindow):
                win.close()
                return
        self.quit()

    def _on_activate(self, *_args) -> None:
        _ensure_main_window(self)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        # Override the vfunc so D-Bus CommandLine is implemented reliably
        # (signal-only handlers can race with an older primary instance).
        argv = command_line.get_arguments()
        log.debug("command-line argv=%r", list(argv))
        win = _ensure_main_window(self)
        if win is None:
            command_line.set_exit_status(1)
            return 1
        mailtos = [
            arg
            for arg in argv[1:]
            if isinstance(arg, str) and arg.lower().startswith("mailto:")
        ]
        if mailtos:

            def _open_mailtos() -> bool:
                for uri in mailtos:
                    log.debug("opening mailto compose for %r", uri)
                    try:
                        win.open_compose_mailto(uri)
                    except Exception:
                        log.exception("Failed to open mailto compose for %r", uri)
                return False

            # Defer until after the main window is mapped; present compose last.
            GLib.idle_add(_open_mailtos)
        command_line.set_exit_status(0)
        return 0


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

    app = PostApplication()
    return app.run(sys.argv)
