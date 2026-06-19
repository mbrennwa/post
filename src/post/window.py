# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main application window — 3-pane mail layout."""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")

from gi.repository import Adw, GLib, Gtk, WebKit

from post.mail import MailService
from post.mail.eds import MailAccount
from post.reader import build_reader_document

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Post")
        self.set_default_size(1100, 720)

        self._mail = MailService.connect()
        self._accounts: list[MailAccount] = []
        self._accounts_by_uid: dict[str, MailAccount] = {}
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._current_body: dict[str, str | None] = {"plain": None, "html": None}
        self._sidebar_selecting = False
        self._expanded_accounts: dict[str, bool] = {}
        self._folder_lists: dict[str, Gtk.ListBox] = {}
        self._messages_load_generation = 0
        self._message_populate_generation = 0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Post", subtitle="Mail")
        header.set_title_widget(title)
        outer.append(header)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_end(refresh_btn)

        remote_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        remote_label = Gtk.Label(label="Remote content")
        remote_label.add_css_class("dim-label")
        self._remote_switch = Gtk.Switch(active=False)
        self._remote_switch.set_tooltip_text("Load remote images and linked resources")
        self._remote_switch.connect("notify::active", self._on_remote_content_changed)
        remote_box.append(remote_label)
        remote_box.append(self._remote_switch)
        header.pack_end(remote_box)

        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        panes.set_vexpand(True)
        outer.append(panes)

        # Sidebar: collapsible section per account
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_size_request(240, -1)
        self._sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._sidebar_box.add_css_class("navigation-sidebar")
        sidebar_scroll.set_child(self._sidebar_box)
        panes.append(sidebar_scroll)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep1)

        # Message list (stack: loading indicator | list)
        self._message_stack = Gtk.Stack()
        self._message_stack.set_size_request(320, -1)
        self._message_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._message_stack.set_transition_duration(150)

        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        loading_box.set_margin_start(24)
        loading_box.set_margin_end(24)
        self._message_loading_spinner = Gtk.Spinner()
        self._message_loading_spinner.set_size_request(32, 32)
        loading_box.append(self._message_loading_spinner)
        self._message_loading_label = Gtk.Label(label="Loading messages…")
        self._message_loading_label.set_wrap(True)
        self._message_loading_label.add_css_class("dim-label")
        loading_box.append(self._message_loading_label)
        self._message_stack.add_named(loading_box, "loading")

        message_scroll = Gtk.ScrolledWindow()
        message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._message_list = Gtk.ListBox()
        self._message_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._message_list.connect("row-selected", self._on_message_selected)
        message_scroll.set_child(self._message_list)
        self._message_stack.add_named(message_scroll, "list")
        self._message_stack.set_visible_child_name("list")

        panes.append(self._message_stack)

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

        self._web_view = WebKit.WebView()
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        self._web_view.set_vexpand(True)
        reader.append(self._web_view)

        panes.append(reader)

        self._status = Gtk.Label(label="", xalign=0, margin_start=12, margin_bottom=6)
        self._status.add_css_class("dim-label")
        outer.append(self._status)

        self._load_sidebar()

    def _set_status(self, text: str) -> None:
        self._status.set_label(text)

    def _load_sidebar(self) -> None:
        self._clear_reader()
        self._clear_listbox(self._message_list)
        self._save_expanded_state()
        self._clear_sidebar()
        self._folder_lists.clear()
        self._current_account = None
        self._current_folder = None

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

        self._accounts_by_uid = {a.uid: a for a in self._accounts}

        for account in self._accounts:
            self._sidebar_box.append(self._make_account_section(account))

        initial_list, initial_row = self._find_initial_folder()
        if initial_list is not None and initial_row is not None:
            self._sidebar_selecting = True
            initial_list.select_row(initial_row)
            self._sidebar_selecting = False

        self._set_status(f"{len(self._accounts)} account(s)")

    def _save_expanded_state(self) -> None:
        for uid, listbox in self._folder_lists.items():
            expander = listbox.get_parent()
            while expander is not None and not isinstance(expander, Gtk.Expander):
                expander = expander.get_parent()
            if isinstance(expander, Gtk.Expander):
                self._expanded_accounts[uid] = expander.get_expanded()

    def _clear_sidebar(self) -> None:
        while child := self._sidebar_box.get_first_child():
            self._sidebar_box.remove(child)

    def _find_initial_folder(self) -> tuple[Gtk.ListBox | None, Gtk.ListBoxRow | None]:
        """Prefer the first INBOX across accounts, else the first folder."""
        first: tuple[Gtk.ListBox | None, Gtk.ListBoxRow | None] = (None, None)
        for listbox in self._folder_lists.values():
            row = listbox.get_first_child()
            while row is not None:
                folder_name = getattr(row, "folder_name", None)
                if folder_name:
                    if first[1] is None:
                        first = (listbox, row)
                    if folder_name.upper() in ("INBOX", "INBOX/"):
                        return listbox, row
                row = row.get_next_sibling()
        return first

    def _make_account_section(self, account: MailAccount) -> Gtk.Expander:
        expander = Gtk.Expander()
        expander.set_expanded(self._expanded_accounts.get(account.uid, True))
        expander.connect("notify::expanded", self._on_account_expanded, account.uid)
        expander.set_label_widget(self._make_account_header(account))
        expander.set_margin_start(6)
        expander.set_margin_end(6)
        expander.set_margin_top(4)

        folder_list = Gtk.ListBox()
        folder_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        folder_list.add_css_class("navigation-sidebar")
        folder_list.connect("row-selected", self._on_folder_selected)
        self._folder_lists[account.uid] = folder_list

        try:
            folders = self._mail.list_folders(account.uid)
        except Exception as exc:
            log.exception("Failed to list folders for %s", account.uid)
            error = Gtk.Label(label=f"Could not load folders: {exc}", xalign=0, wrap=True)
            error.add_css_class("dim-label")
            error.set_margin_start(12)
            error.set_margin_end(12)
            error.set_margin_bottom(8)
            folder_list.append(self._wrap_list_row(error))
            expander.set_child(folder_list)
            return expander

        for folder in folders:
            folder_list.append(self._make_folder_row(account.uid, folder))

        expander.set_child(folder_list)
        return expander

    def _on_account_expanded(self, expander: Gtk.Expander, _pspec, account_uid: str) -> None:
        self._expanded_accounts[account_uid] = expander.get_expanded()

    def _make_account_header(self, account: MailAccount) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        name = Gtk.Label(label=account.name, xalign=0)
        name.add_css_class("heading")
        box.append(name)

        if account.email:
            email = Gtk.Label(label=account.email, xalign=0)
            email.add_css_class("dim-label")
            email.set_ellipsize(3)
            box.append(email)

        return box

    def _wrap_list_row(self, widget: Gtk.Widget) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_child(widget)
        return row

    def _make_folder_row(self, account_uid: str, folder: dict) -> Gtk.ListBoxRow:
        display = folder.get("display_name") or folder.get("full_name") or "?"
        unread = folder.get("unread", -1)
        label_text = f"{display} ({unread})" if unread >= 0 else display

        label = Gtk.Label(label=label_text, xalign=0, margin_start=12, margin_end=12)
        row = Gtk.ListBoxRow()
        row.set_child(label)
        row.account_uid = account_uid
        row.folder_name = folder.get("full_name")
        return row

    def _clear_reader(self) -> None:
        self._reader_subject.set_label("Select a message")
        self._reader_meta.set_label("")
        self._current_body = {"plain": None, "html": None}
        self._show_reader_document()

    def _on_refresh(self, *_args) -> None:
        if self._current_account and self._current_folder:
            self._load_messages(self._current_account.uid, self._current_folder)
        else:
            self._load_sidebar()

    def _clear_listbox(self, listbox: Gtk.ListBox) -> None:
        while child := listbox.get_first_child():
            listbox.remove(child)

    def _on_folder_selected(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._sidebar_selecting:
            return
        account_uid = getattr(row, "account_uid", None)
        folder_name = getattr(row, "folder_name", None)
        if not account_uid or not folder_name:
            return

        # Keep a single selection across account folder lists.
        for uid, other in self._folder_lists.items():
            if other is not listbox:
                other.unselect_all()

        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            return

        self._current_account = account
        self._current_folder = folder_name
        self._load_messages(account_uid, folder_name)

    def _load_messages(self, account_uid: str, folder_name: str) -> None:
        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            return

        self._messages_load_generation += 1
        load_id = self._messages_load_generation
        self._message_populate_generation += 1

        self._set_status(f"Loading {folder_name}…")
        self._clear_listbox(self._message_list)
        self._clear_reader()
        self._message_loading_label.set_label(f"Loading {folder_name}…")
        self._message_loading_spinner.start()
        self._message_stack.set_visible_child_name("loading")

        def worker() -> None:
            error: Exception | None = None
            messages: list[dict] | None = None
            try:
                messages = self._mail.list_messages(account_uid, folder_name)
            except Exception as exc:
                log.exception("Failed to list messages")
                error = exc
            GLib.idle_add(
                self._on_messages_loaded,
                load_id,
                account_uid,
                folder_name,
                messages,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_messages_loaded(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        messages: list[dict] | None,
        error: Exception | None,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        self._message_loading_spinner.stop()

        if error is not None:
            self._message_stack.set_visible_child_name("list")
            self._set_status(f"Message error: {error}")
            return False

        assert messages is not None
        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            self._message_stack.set_visible_child_name("list")
            return False

        self._message_stack.set_visible_child_name("list")
        self._message_populate_generation += 1
        populate_id = self._message_populate_generation
        self._populate_message_rows(populate_id, messages, 0, account, folder_name)
        return False

    def _populate_message_rows(
        self,
        populate_id: int,
        messages: list[dict],
        offset: int,
        account: MailAccount,
        folder_name: str,
        batch_size: int = 25,
    ) -> bool:
        if populate_id != self._message_populate_generation:
            return False

        end = min(offset + batch_size, len(messages))
        for msg in messages[offset:end]:
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
            meta.set_ellipsize(3)
            meta.add_css_class("dim-label")
            preview.append(meta)

            row = Gtk.ListBoxRow()
            row.set_child(preview)
            row.message_uid = msg.get("uid")
            self._message_list.append(row)

        if end < len(messages):
            GLib.idle_add(
                self._populate_message_rows,
                populate_id,
                messages,
                end,
                account,
                folder_name,
                batch_size,
            )
            self._set_status(f"Loading {folder_name}… ({end}/{len(messages)})")
        else:
            display = account.name
            self._set_status(f"{len(messages)} messages in {display} / {folder_name}")

        return False

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
        self._current_body = {
            "plain": msg.get("body_plain"),
            "html": msg.get("body_html"),
        }
        self._show_reader_document()

    def _show_reader_document(self) -> None:
        document = build_reader_document(
            body_html=self._current_body.get("html"),
            body_plain=self._current_body.get("plain"),
            allow_remote=self._remote_switch.get_active(),
        )
        self._web_view.load_html(document, None)

    def _on_remote_content_changed(self, *_args) -> None:
        if self._current_body.get("html") or self._current_body.get("plain"):
            self._show_reader_document()
