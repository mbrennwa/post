# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Password prompt for IMAP accounts without stored credentials."""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)

PasswordPrompt = Gtk.Window | Adw.ApplicationWindow | Adw.Window


def prompt_password_sync(parent: PasswordPrompt, account_label: str) -> str | None:
    """Show a modal password dialog; safe to call from a worker thread."""
    if threading.current_thread() is threading.main_thread():
        return _show_password_dialog(parent, account_label)

    result: dict[str, str | None] = {"password": None}
    done = threading.Event()

    def on_idle() -> bool:
        result["password"] = _show_password_dialog(parent, account_label)
        done.set()
        return False

    GLib.idle_add(on_idle)
    done.wait()
    return result["password"]


def _show_password_dialog(parent: PasswordPrompt, account_label: str) -> str | None:
    result: dict[str, str | None] = {"password": None}
    loop = GLib.MainLoop()

    parent.present()
    log.info("Showing password sign-in dialog for %s", account_label)

    entry = Gtk.PasswordEntry()
    entry.props.placeholder_text = "Password"
    entry.props.activates_default = True

    dialog = Adw.MessageDialog(
        transient_for=parent,
        modal=True,
    )
    dialog.set_heading(f"Sign in to {account_label}")
    dialog.set_body("Enter the password for this email account.")
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("login", "Sign In")
    dialog.set_default_response("login")
    dialog.set_close_response("cancel")
    dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
        if response == "login":
            text = entry.get_text()
            if text:
                result["password"] = text
        loop.quit()

    dialog.connect("response", on_response)
    dialog.present()
    loop.run()
    return result["password"]
