# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dialogs for sidebar folder management."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import Adw, GLib, Gtk

DialogParent = Gtk.Window | Adw.ApplicationWindow | Adw.Window


def prompt_folder_name(
    parent: DialogParent,
    *,
    heading: str,
    body: str,
    initial: str = "",
    confirm_label: str = "Create",
) -> str | None:
    result: dict[str, str | None] = {"value": None}
    loop = GLib.MainLoop()

    entry = Gtk.Entry()
    entry.set_text(initial)
    entry.set_placeholder_text("Folder name")
    entry.set_activates_default(True)

    dialog = Adw.MessageDialog(transient_for=parent, modal=True)
    dialog.set_heading(heading)
    dialog.set_body(body)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    dialog.set_default_response("confirm")
    dialog.set_close_response("cancel")
    dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
        if response == "confirm":
            text = entry.get_text().strip()
            if text:
                result["value"] = text
        loop.quit()

    dialog.connect("response", on_response)
    dialog.present()
    loop.run()
    return result["value"]


def confirm_action(
    parent: DialogParent,
    *,
    heading: str,
    body: str,
    confirm_label: str = "Confirm",
    destructive: bool = False,
) -> bool:
    result = {"confirmed": False}
    loop = GLib.MainLoop()

    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    if destructive:
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        result["confirmed"] = response == "confirm"
        loop.quit()

    dialog.connect("response", on_response)
    dialog.present(parent)
    loop.run()
    return result["confirmed"]


def show_error(parent: DialogParent, heading: str, body: str) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    dialog.present(parent)
