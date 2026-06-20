# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Application settings."""

from __future__ import annotations

import logging
import os
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
    default_spool_path,
    is_builtin_local_store_empty,
    read_local_mail_config,
    validate_local_mail_config,
)
from post.preferences import (
    get_load_remote_content,
    get_show_evolution_local,
    set_load_remote_content,
    set_show_evolution_local,
)

log = logging.getLogger(__name__)

SetStatus = Callable[[str], None]
OnSaved = Callable[[], None]
OnLoadRemoteContentChanged = Callable[[bool], None]


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        set_status: SetStatus,
        on_saved: OnSaved,
        on_load_remote_content_changed: OnLoadRemoteContentChanged | None = None,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._mail = mail
        self._set_status = set_status
        self._on_saved = on_saved
        self._remote_content_changed_callback = on_load_remote_content_changed
        self._saving = False
        self._loading_settings = True

        existing = read_local_mail_config(mail.registry)
        self._config = existing or default_local_mail_config()

        self.add(self._build_reading_page())
        self.add(self._build_composing_page())
        self.add(self._build_local_mail_page())
        self._loading_settings = False

    def _build_reading_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Reading")
        page.set_icon_name("mail-read-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("Message display")

        self._remote_content_row = Adw.SwitchRow(title="Load remote content")
        self._remote_content_row.set_subtitle(
            "Show remote images and linked resources in HTML messages"
        )
        self._remote_content_row.set_active(get_load_remote_content())
        self._remote_content_row.connect(
            "notify::active", self._on_remote_content_row_changed
        )
        group.add(self._remote_content_row)
        page.add(group)
        return page

    def _build_composing_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Composing")
        page.set_icon_name("mail-message-new-symbolic")

        group = Adw.PreferencesGroup()
        group.set_description("Compose options will appear here.")
        page.add(group)
        return page

    def _build_local_mail_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Local Mail")
        page.set_icon_name("computer-symbolic")

        evolution_group = Adw.PreferencesGroup()
        evolution_group.set_title("On This Computer")
        evolution_group.set_description(
            "Evolution’s built-in local mail store (Maildir under "
            "~/.local/share/evolution/mail/local)"
        )

        self._evolution_local_row = Adw.SwitchRow(title="Show in Post")
        pref = get_show_evolution_local()
        if pref is None:
            self._evolution_local_row.set_active(
                not is_builtin_local_store_empty(self._mail.registry)
            )
        else:
            self._evolution_local_row.set_active(pref)
        self._evolution_local_row.connect(
            "notify::active", self._on_evolution_local_changed
        )
        evolution_group.add(self._evolution_local_row)
        page.add(evolution_group)

        spool_group = Adw.PreferencesGroup()
        spool_group.set_title("System mail")
        spool_group.set_description(
            "Spool file or Maildir used by mutt, fetchmail, or similar tools"
        )

        self._enable_row = Adw.SwitchRow(title="Enable system local mail")
        self._enable_row.set_active(self._config.enabled)
        spool_group.add(self._enable_row)

        self._type_row = Adw.ComboRow(title="Storage type")
        types = Gtk.StringList.new(["Spool file (mbox)", "Maildir folder"])
        self._type_row.set_model(types)
        self._type_row.set_selected(0 if self._config.mail_type == "spool" else 1)
        spool_group.add(self._type_row)

        self._path_entry = Adw.EntryRow(title="Path")
        self._path_entry.set_text(self._config.path)
        browse_btn = Gtk.Button(label="Browse…")
        browse_btn.connect("clicked", self._on_browse_clicked)
        self._path_entry.add_suffix(browse_btn)
        spool_group.add(self._path_entry)
        page.add(spool_group)

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

        self._type_row.connect("notify::selected", self._on_type_changed)
        self._on_type_changed()
        return page

    def _on_type_changed(self, *_args) -> None:
        is_spool = self._type_row.get_selected() == 0
        subtitle = (
            "Mbox spool file (e.g. /var/spool/mail/$USER)"
            if is_spool
            else "Maildir folder (contains cur/, new/, tmp/)"
        )
        self._path_entry.set_title("Path")
        self._type_row.set_subtitle(subtitle)

    def _on_evolution_local_changed(self, *_args) -> None:
        if self._loading_settings:
            return
        set_show_evolution_local(self._evolution_local_row.get_active())
        self._on_saved()

    def _on_remote_content_row_changed(self, *_args) -> None:
        if self._loading_settings:
            return
        enabled = self._remote_content_row.get_active()
        set_load_remote_content(enabled)
        if self._remote_content_changed_callback is not None:
            self._remote_content_changed_callback(enabled)

    def _current_config(self) -> LocalMailConfig:
        return LocalMailConfig(
            enabled=self._enable_row.get_active(),
            mail_type="spool" if self._type_row.get_selected() == 0 else "maildir",
            path=self._path_entry.get_text().strip(),
            from_name=self._name_row.get_text().strip(),
            from_address=self._address_row.get_text().strip(),
        )

    def _browse_start_path(self, *, for_spool: bool) -> str:
        current = self._path_entry.get_text().strip()
        if current:
            return current
        if for_spool:
            return default_spool_path()
        return os.path.expanduser("~/Maildir")

    @staticmethod
    def _configure_file_dialog_start(
        dialog: Gtk.FileDialog, path: str, *, pick_file: bool
    ) -> None:
        if pick_file and os.path.isfile(path):
            dialog.set_initial_file(Gio.File.new_for_path(path))
            return

        folder_path = path if os.path.isdir(path) else os.path.dirname(path)
        if folder_path and os.path.isdir(folder_path):
            dialog.set_initial_folder(Gio.File.new_for_path(folder_path))
            if pick_file:
                name = os.path.basename(path)
                if name:
                    dialog.set_initial_name(name)
            return

        fallback = "/var/spool/mail" if pick_file else os.path.expanduser("~")
        if os.path.isdir(fallback):
            dialog.set_initial_folder(Gio.File.new_for_path(fallback))

    def _on_browse_clicked(self, _button: Gtk.Button) -> None:
        is_spool = self._type_row.get_selected() == 0
        dialog = Gtk.FileDialog(
            title="Choose spool file" if is_spool else "Choose Maildir folder"
        )
        self._configure_file_dialog_start(
            dialog, self._browse_start_path(for_spool=is_spool), pick_file=is_spool
        )

        def on_selected(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = _dialog.open_finish(result)
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
            dialog.open(self._parent, None, on_selected)
        else:
            dialog.select_folder(self._parent, None, on_folder_selected)

    def _on_save_clicked(self, *_args) -> None:
        if self._saving:
            return

        config = self._current_config()
        show_evolution_local = self._evolution_local_row.get_active()
        if config.enabled:
            error = validate_local_mail_config(config)
            if error:
                self._show_error(error)
                return

        self._saving = True
        self.set_can_close(False)
        self._set_status("Saving settings…")

        def worker() -> None:
            save_error: Exception | None = None
            try:
                set_show_evolution_local(show_evolution_local)
                apply_local_mail_config(config)
                self._mail.reload_registry()
            except Exception as exc:
                log.exception("Failed to save settings")
                save_error = exc
            GLib.idle_add(self._on_save_finished, save_error, show_evolution_local)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_finished(
        self, error: Exception | None, show_evolution_local: bool
    ) -> bool:
        self._saving = False
        self.set_can_close(True)
        if error is not None:
            set_show_evolution_local(show_evolution_local)
            self._on_saved()
            self._show_error(str(error))
            self._set_status(f"Could not save system mail settings: {error}")
            return False

        self._set_status("Settings saved")
        self._on_saved()
        self.force_close()
        return False

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Could not save settings",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self._parent)


# Backwards-compatible alias for imports.
SettingsWindow = SettingsDialog
