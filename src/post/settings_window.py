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
    EDS_LOCAL_DISPLAY_NAME,
    LocalMailConfig,
    apply_local_mail_config,
    default_local_mail_config,
    default_spool_path,
    is_builtin_local_store_empty,
    read_local_mail_config,
    validate_local_mail_config,
)
from post.preferences import (
    get_account_signature,
    get_auto_sync,
    get_load_remote_content,
    get_show_evolution_local,
    set_account_signature,
    set_auto_sync,
    set_load_remote_content,
    set_show_evolution_local,
)

log = logging.getLogger(__name__)

SetStatus = Callable[[str], None]
OnSaved = Callable[[], None]
OnLoadRemoteContentChanged = Callable[[bool], None]
OnAutoSyncChanged = Callable[[bool], None]


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        set_status: SetStatus,
        on_saved: OnSaved,
        on_load_remote_content_changed: OnLoadRemoteContentChanged | None = None,
        on_auto_sync_changed: OnAutoSyncChanged | None = None,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._mail = mail
        self._set_status = set_status
        self._on_saved = on_saved
        self._remote_content_changed_callback = on_load_remote_content_changed
        self._auto_sync_changed_callback = on_auto_sync_changed
        self._saving = False
        self._loading_settings = True
        self._local_mail_save_id: int | None = None

        existing = read_local_mail_config(mail.registry)
        self._config = existing or default_local_mail_config()

        self.add(self._build_reading_page())
        self.add(self._build_composing_page())
        self.add(self._build_local_mail_page())
        self._loading_settings = False
        self.connect("closed", self._on_signature_dialog_closed)

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

        self._auto_sync_row = Adw.SwitchRow(title="Auto sync")
        self._auto_sync_row.set_subtitle(
            "Check for new mail and server-side changes"
        )
        self._auto_sync_row.set_active(get_auto_sync())
        self._auto_sync_row.connect("notify::active", self._on_auto_sync_row_changed)
        group.add(self._auto_sync_row)
        page.add(group)
        return page

    def _build_composing_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Composing")
        page.set_icon_name("mail-message-new-symbolic")

        group = Adw.PreferencesGroup()
        group.set_title("Signatures")
        group.set_description(
            "Plain-text signatures added when composing from each account"
        )

        self._signature_accounts = self._mail.list_sendable_accounts()
        self._signature_account_uid: str | None = None
        self._signature_loading = False

        if not self._signature_accounts:
            empty_row = Adw.ActionRow(title="No sendable accounts")
            empty_row.set_subtitle(
                "Configure a mail account with outgoing mail to set signatures"
            )
            group.add(empty_row)
            page.add(group)
            self._signature_buffer = None
            self._signature_view = None
            self._signature_account_row = None
            return page

        labels = [account.from_label for account in self._signature_accounts]
        self._signature_account_row = Adw.ComboRow(title="Account")
        self._signature_account_row.set_model(Gtk.StringList.new(labels))
        self._signature_account_row.set_selected(0)
        self._signature_account_row.connect(
            "notify::selected", self._on_signature_account_changed
        )
        group.add(self._signature_account_row)

        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        editor_box.set_margin_start(12)
        editor_box.set_margin_end(12)
        editor_box.set_margin_top(6)
        editor_box.set_margin_bottom(12)

        editor_label = Gtk.Label(
            label="Signature",
            xalign=0,
        )
        editor_label.add_css_class("heading")
        editor_box.append(editor_label)

        editor_frame = Gtk.Frame()
        editor_frame.add_css_class("view")
        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        editor_scroll.set_min_content_height(120)
        self._signature_view = Gtk.TextView()
        self._signature_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._signature_view.set_left_margin(8)
        self._signature_view.set_right_margin(8)
        self._signature_view.set_top_margin(8)
        self._signature_view.set_bottom_margin(8)
        editor_scroll.set_child(self._signature_view)
        editor_frame.set_child(editor_scroll)
        editor_box.append(editor_frame)

        group.add(editor_box)
        page.add(group)

        self._signature_buffer = self._signature_view.get_buffer()
        self._signature_buffer.connect("changed", self._on_signature_buffer_changed)
        self._load_signature_for_selected_account()
        return page

    def _selected_signature_account_uid(self) -> str | None:
        if self._signature_account_row is None:
            return None
        index = self._signature_account_row.get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return None
        return self._signature_accounts[index].uid

    def _load_signature_for_selected_account(self) -> None:
        if self._signature_buffer is None:
            return
        self._signature_loading = True
        uid = self._selected_signature_account_uid()
        self._signature_account_uid = uid
        text = get_account_signature(uid) if uid else ""
        self._signature_buffer.set_text(text)
        self._signature_loading = False

    def _persist_current_signature(self) -> None:
        if self._signature_buffer is None or self._signature_account_uid is None:
            return
        start, end = self._signature_buffer.get_bounds()
        text = self._signature_buffer.get_text(start, end, False)
        set_account_signature(self._signature_account_uid, text)

    def _on_signature_account_changed(self, *_args) -> None:
        if self._signature_loading:
            return
        self._persist_current_signature()
        self._load_signature_for_selected_account()

    def _on_signature_buffer_changed(self, *_args) -> None:
        if self._signature_loading:
            return
        self._persist_current_signature()

    def _on_signature_dialog_closed(self, *_args) -> None:
        self._persist_current_signature()

    def _build_local_mail_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        page.set_title("Local Mail")
        page.set_icon_name("computer-symbolic")

        spool_group = Adw.PreferencesGroup()
        spool_group.set_title("System mail")
        spool_group.set_description("Mail file or folder")

        self._enable_row = Adw.SwitchRow(title="Enable system mail")
        self._enable_row.set_active(self._config.enabled)
        self._enable_row.connect("notify::active", self._on_system_mail_changed)
        spool_group.add(self._enable_row)

        self._type_row = Adw.ComboRow(title="Storage")
        types = Gtk.StringList.new(["Single mail file", "Folder of messages"])
        self._type_row.set_model(types)
        self._type_row.set_selected(0 if self._config.mail_type == "spool" else 1)
        self._type_row.connect("notify::selected", self._on_system_mail_changed)
        spool_group.add(self._type_row)

        self._path_entry = Adw.EntryRow(title="Path")
        self._path_entry.set_text(self._config.path)
        self._path_entry.connect("changed", self._on_system_mail_entry_changed)
        self._path_entry.connect("apply", self._on_system_mail_apply)
        browse_btn = Gtk.Button(label="Browse…")
        browse_btn.connect("clicked", self._on_browse_clicked)
        self._path_entry.add_suffix(browse_btn)
        spool_group.add(self._path_entry)

        self._name_row = Adw.EntryRow(title="From name")
        self._name_row.set_text(self._config.from_name)
        self._name_row.connect("changed", self._on_system_mail_entry_changed)
        self._name_row.connect("apply", self._on_system_mail_apply)
        spool_group.add(self._name_row)

        self._address_row = Adw.EntryRow(title="From address")
        self._address_row.set_text(self._config.from_address)
        self._address_row.connect("changed", self._on_system_mail_entry_changed)
        self._address_row.connect("apply", self._on_system_mail_apply)
        spool_group.add(self._address_row)
        page.add(spool_group)

        evolution_group = Adw.PreferencesGroup()
        evolution_group.set_title(EDS_LOCAL_DISPLAY_NAME)
        evolution_group.set_description(
            "Local mail at ~/.local/share/evolution/mail/local"
        )

        self._evolution_local_row = Adw.SwitchRow(title="Enable Evolution Data Server")
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

        self._type_row.connect("notify::selected", self._on_type_changed)
        self._on_type_changed()
        return page

    def _on_type_changed(self, *_args) -> None:
        is_spool = self._type_row.get_selected() == 0
        subtitle = (
            "All messages in one file (e.g. /var/spool/mail/yourname)"
            if is_spool
            else "Each message stored as its own file in a folder"
        )
        self._path_entry.set_title("Path")
        self._type_row.set_subtitle(subtitle)

    def _on_system_mail_changed(self, *_args) -> None:
        if self._loading_settings:
            return
        self._save_local_mail_config()

    def _on_system_mail_entry_changed(self, *_args) -> None:
        if self._loading_settings:
            return
        self._schedule_local_mail_save()

    def _on_system_mail_apply(self, *_args) -> None:
        if self._loading_settings:
            return
        self._cancel_local_mail_save()
        self._save_local_mail_config()

    def _schedule_local_mail_save(self) -> None:
        self._cancel_local_mail_save()

        def fire() -> bool:
            self._local_mail_save_id = None
            self._save_local_mail_config()
            return False

        self._local_mail_save_id = GLib.timeout_add(500, fire)

    def _cancel_local_mail_save(self) -> None:
        if self._local_mail_save_id is not None:
            GLib.source_remove(self._local_mail_save_id)
            self._local_mail_save_id = None

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

    def _on_auto_sync_row_changed(self, *_args) -> None:
        if self._loading_settings:
            return
        enabled = self._auto_sync_row.get_active()
        set_auto_sync(enabled)
        if self._auto_sync_changed_callback is not None:
            self._auto_sync_changed_callback(enabled)

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
            title="Choose mail file" if is_spool else "Choose mail folder"
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
                self._save_local_mail_config()

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
                self._save_local_mail_config()

        if is_spool:
            dialog.open(self._parent, None, on_selected)
        else:
            dialog.select_folder(self._parent, None, on_folder_selected)

    def _save_local_mail_config(self) -> None:
        if self._loading_settings or self._saving:
            return

        config = self._current_config()
        if config.enabled:
            error = validate_local_mail_config(config)
            if error:
                self._show_error(error)
                return

        self._saving = True
        self._set_status("Saving settings…")

        def worker() -> None:
            save_error: Exception | None = None
            try:
                apply_local_mail_config(config)
                self._mail.reload_registry()
            except Exception as exc:
                log.exception("Failed to save system mail settings")
                save_error = exc
            GLib.idle_add(self._on_local_mail_save_finished, save_error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_local_mail_save_finished(self, error: Exception | None) -> bool:
        self._saving = False
        if error is not None:
            self._show_error(str(error))
            self._set_status(f"Could not save system mail settings: {error}")
            return False

        self._set_status("Settings saved")
        self._on_saved()
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
