# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Application settings — local mail account setup."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, GLib, Gio, Gtk

from post.mail import MailService
from post.mail.accounts import (
    LocalMailConfig,
    apply_local_mail_config,
    default_local_mail_config,
    read_local_mail_config,
    validate_local_mail_config,
)

log = logging.getLogger(__name__)

SetStatus = Callable[[str], None]
OnSaved = Callable[[], None]


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        set_status: SetStatus,
        on_saved: OnSaved,
    ) -> None:
        super().__init__(transient_for=parent, modal=True)
        self.set_title("Settings")
        self.set_default_size(480, 420)
        self._mail = mail
        self._set_status = set_status
        self._on_saved = on_saved
        self._saving = False

        existing = read_local_mail_config(mail.registry)
        self._config = existing or default_local_mail_config()

        page = Adw.PreferencesPage()
        page.set_title("Local mail")
        page.set_icon_name("mail-unread-symbolic")

        mail_group = Adw.PreferencesGroup()
        mail_group.set_title("Storage")
        mail_group.set_description(
            "Read mail from a system spool file or Maildir folder"
        )

        self._enable_row = Adw.SwitchRow(title="Enable local mail")
        self._enable_row.set_active(self._config.enabled)
        mail_group.add(self._enable_row)

        self._type_row = Adw.ComboRow(title="Storage type")
        types = Gtk.StringList.new(["Spool file (mbox)", "Maildir folder"])
        self._type_row.set_model(types)
        self._type_row.set_selected(0 if self._config.mail_type == "spool" else 1)
        mail_group.add(self._type_row)

        self._path_entry = Adw.EntryRow(title="Path")
        self._path_entry.set_text(self._config.path)
        browse_btn = Gtk.Button(label="Browse…")
        browse_btn.connect("clicked", self._on_browse_clicked)
        self._path_entry.add_suffix(browse_btn)
        mail_group.add(self._path_entry)
        page.add(mail_group)

        identity_group = Adw.PreferencesGroup()
        identity_group.set_title("Identity")
        identity_group.set_description("Used when composing from this account")

        self._name_row = Adw.EntryRow(title="From name")
        self._name_row.set_text(self._config.from_name)
        identity_group.add(self._name_row)

        self._address_row = Adw.EntryRow(title="From address")
        self._address_row.set_text(self._config.from_address)
        identity_group.add(self._address_row)
        page.add(identity_group)

        actions_group = Adw.PreferencesGroup()
        save_row = Adw.ActionRow(title="Save settings")
        save_row.set_activatable(True)
        save_row.connect("activated", self._on_save_clicked)
        actions_group.add(save_row)
        page.add(actions_group)

        self.add(page)

        self._type_row.connect("notify::selected", self._on_type_changed)
        self._on_type_changed()

    def _on_type_changed(self, *_args) -> None:
        is_spool = self._type_row.get_selected() == 0
        subtitle = (
            "Mbox spool file (e.g. /var/spool/mail/$USER)"
            if is_spool
            else "Maildir folder (contains cur/, new/, tmp/)"
        )
        self._path_entry.set_title("Path")
        self._type_row.set_subtitle(subtitle)

    def _current_config(self) -> LocalMailConfig:
        return LocalMailConfig(
            enabled=self._enable_row.get_active(),
            mail_type="spool" if self._type_row.get_selected() == 0 else "maildir",
            path=self._path_entry.get_text().strip(),
            from_name=self._name_row.get_text().strip(),
            from_address=self._address_row.get_text().strip(),
        )

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose mail location")
        parent = self.get_transient_for()
        is_spool = self._type_row.get_selected() == 0

        def on_selected(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = _dialog.select_file_finish(result)
            except GLib.Error:
                return
            if file is None:
                return
            path = file.get_path()
            if path:
                self._path_entry.set_text(path)

        def on_folder_selected(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = _dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if file is None:
                return
            path = file.get_path()
            if path:
                self._path_entry.set_text(path)

        if is_spool:
            dialog.select_file(parent, None, on_selected)
        else:
            dialog.select_folder(parent, None, on_folder_selected)

    def _on_save_clicked(self, *_args) -> None:
        if self._saving:
            return

        config = self._current_config()
        error = validate_local_mail_config(config)
        if error:
            self._show_error(error)
            return

        self._saving = True
        self._set_status("Saving local mail settings…")

        def worker() -> None:
            save_error: Exception | None = None
            try:
                apply_local_mail_config(config)
                self._mail.reload_registry()
            except Exception as exc:
                log.exception("Failed to save local mail settings")
                save_error = exc
            GLib.idle_add(self._on_save_finished, save_error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_finished(self, error: Exception | None) -> bool:
        self._saving = False
        if error is not None:
            self._show_error(str(error))
            self._set_status(f"Could not save settings: {error}")
            return False

        self._set_status("Local mail settings saved")
        self._on_saved()
        self.close()
        return False

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Could not save settings",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)
