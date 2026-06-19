# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main application window — 3-pane mail layout."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from post.mail import MailService
from post.mail.eds import MailAccount

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Post")
        self.set_default_size(1100, 720)

        self._mail = MailService.connect()
        self._accounts: list[MailAccount] = []
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._folders: list[dict] = []

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)

        header = Adw.HeaderBar()
        outer.append(header)

        self._account_dropdown = Gtk.DropDown.new(Gtk.StringList.new([]), None)
        self._account_dropdown.connect("notify::selected", self._on_account_changed)
        header.set_title_widget(self._account_dropdown)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_end(refresh_btn)

        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        panes.set_vexpand(True)
        outer.append(panes)

        # Folder sidebar
        folder_scroll = Gtk.ScrolledWindow()
        folder_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        folder_scroll.set_size_request(200, -1)
        self._folder_list = Gtk.ListBox()
        self._folder_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._folder_list.connect("row-selected", self._on_folder_selected)
        self._folder_list.add_css_class("navigation-sidebar")
        folder_scroll.set_child(self._folder_list)
        panes.append(folder_scroll)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep1)

        # Message list
        message_scroll = Gtk.ScrolledWindow()
        message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        message_scroll.set_size_request(320, -1)
        self._message_list = Gtk.ListBox()
        self._message_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._message_list.connect("row-selected", self._on_message_selected)
        message_scroll.set_child(self._message_list)
        panes.append(message_scroll)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep2)

        # Reading pane
        reader = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        reader.set_hexpand(True)
        reader.set_margin_start(16)
        reader.set_margin_end(16)
        reader.set_margin_top(12)
        reader.set_margin_bottom(12)

        self._reader_subject = Gtk.Label(
            label="Select a message",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._reader_subject.add_css_class("title-2")
        reader.append(self._reader_subject)

        self._reader_meta = Gtk.Label(label="", xalign=0, wrap=True)
        self._reader_meta.add_css_class("dim-label")
        reader.append(self._reader_meta)

        reader_scroll = Gtk.ScrolledWindow()
        reader_scroll.set_vexpand(True)
        reader_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._reader_body = Gtk.TextView()
        self._reader_body.set_editable(False)
        self._reader_body.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._reader_body.add_css_class("monospace")
        reader_scroll.set_child(self._reader_body)
        reader.append(reader_scroll)

        panes.append(reader)

        self._status = Gtk.Label(label="", xalign=0, margin_start=12, margin_bottom=6)
        self._status.add_css_class("dim-label")
        outer.append(self._status)

        self._load_accounts()

    def _set_status(self, text: str) -> None:
        self._status.set_label(text)

    def _load_accounts(self) -> None:
        try:
            self._accounts = self._mail.list_accounts()
        except Exception as exc:
            log.exception("Failed to list mail accounts")
            self._set_status(f"Error: {exc}")
            return

        if not self._accounts:
            self._set_status(
                "No mail accounts found. Add one in Evolution or GNOME Online Accounts first."
            )
            return

        names = Gtk.StringList.new(
            [f"{a.name} ({a.email or 'no address'})" for a in self._accounts]
        )
        self._account_dropdown.set_model(names)
        self._account_dropdown.set_selected(0)
        self._set_status(f"{len(self._accounts)} account(s)")

    def _on_account_changed(self, _dropdown, _pspec) -> None:
        index = self._account_dropdown.get_selected()
        if index >= len(self._accounts):
            return
        self._current_account = self._accounts[index]
        self._load_folders()

    def _on_refresh(self, *_args) -> None:
        if self._current_account and self._current_folder:
            self._load_messages(self._current_folder)
        elif self._current_account:
            self._load_folders()

    def _clear_listbox(self, listbox: Gtk.ListBox) -> None:
        while child := listbox.get_first_child():
            listbox.remove(child)

    def _load_folders(self) -> None:
        if not self._current_account:
            return
        account = self._current_account
        self._set_status(f"Loading folders for {account.name}…")
        self._clear_listbox(self._folder_list)
        self._clear_listbox(self._message_list)
        self._reader_subject.set_label("Select a message")
        self._reader_meta.set_label("")
        self._reader_body.get_buffer().set_text("")

        try:
            self._folders = self._mail.list_folders(account.uid)
        except Exception as exc:
            log.exception("Failed to list folders")
            self._set_status(f"Folder error: {exc}")
            return

        for folder in self._folders:
            display = folder.get("display_name") or folder.get("full_name") or "?"
            unread = folder.get("unread", -1)
            label = f"{display} ({unread})" if unread >= 0 else display
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label=label, xalign=0, margin_start=12, margin_end=12))
            row.folder_name = folder.get("full_name")
            self._folder_list.append(row)

        inbox = MailService.guess_inbox(self._folders)
        if inbox:
            self._select_folder_row(inbox)
        self._set_status(f"{len(self._folders)} folders")

    def _select_folder_row(self, folder_name: str) -> None:
        row = self._folder_list.get_first_child()
        while row is not None:
            if getattr(row, "folder_name", None) == folder_name:
                self._folder_list.select_row(row)
                return
            row = row.get_next_sibling()

    def _on_folder_selected(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or not self._current_account:
            return
        folder_name = getattr(row, "folder_name", None)
        if not folder_name:
            return
        self._current_folder = folder_name
        self._load_messages(folder_name)

    def _load_messages(self, folder_name: str) -> None:
        if not self._current_account:
            return
        account = self._current_account
        self._set_status(f"Loading {folder_name}…")
        self._clear_listbox(self._message_list)

        try:
            messages = self._mail.list_messages(account.uid, folder_name)
        except Exception as exc:
            log.exception("Failed to list messages")
            self._set_status(f"Message error: {exc}")
            return

        for msg in messages:
            subject = msg.get("subject") or "(no subject)"
            sender = msg.get("from") or ""
            preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            preview.set_margin_start(12)
            preview.set_margin_end(12)
            preview.set_margin_top(8)
            preview.set_margin_bottom(8)

            subject_label = Gtk.Label(label=subject, xalign=0, wrap=True)
            subject_label.add_css_class("heading")
            preview.append(subject_label)

            meta = Gtk.Label(label=sender, xalign=0)
            meta.set_ellipsize(3)  # Pango.EllipsizeMode.END
            meta.add_css_class("dim-label")
            preview.append(meta)

            row = Gtk.ListBoxRow()
            row.set_child(preview)
            row.message_uid = msg.get("uid")
            self._message_list.append(row)

        self._set_status(f"{len(messages)} messages in {folder_name}")

    def _on_message_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None or not self._current_account or not self._current_folder:
            return
        uid = getattr(row, "message_uid", None)
        if not uid:
            return

        try:
            msg = self._mail.read_message(
                self._current_account.uid, self._current_folder, uid
            )
        except Exception as exc:
            log.exception("Failed to read message")
            self._set_status(f"Read error: {exc}")
            return

        self._reader_subject.set_label(msg.get("subject") or "(no subject)")
        self._reader_meta.set_label(
            f"From: {msg.get('from', '')}\nDate: {msg.get('date_received') or msg.get('date_sent') or ''}"
        )
        body = msg.get("body_plain") or "(No plain-text body — HTML rendering comes later.)"
        self._reader_body.get_buffer().set_text(body)
