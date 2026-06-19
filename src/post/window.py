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

from post.credentials import prompt_password_sync
from post.mail import MailService
from post.mail.eds import DEFAULT_MESSAGE_PAGE_SIZE, MailAccount
from post.reader import build_reader_document
from post.sidebar import MailSidebar

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Post")
        self.set_default_size(1100, 720)

        self._mail = MailService.connect()
        self._mail.set_password_prompt(self._prompt_account_password)
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._current_body: dict[str, str | None] = {"plain": None, "html": None}
        self._messages_load_generation = 0
        self._shown_message_count = 0
        self._message_total = -1
        self._message_has_more = False

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

        self._sidebar = MailSidebar(
            self._mail,
            on_folder_selected=self._on_folder_selected,
            set_status=self._set_status,
        )
        panes.append(self._sidebar.widget)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep1)

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
        message_scroll.set_vexpand(True)
        self._message_list = Gtk.ListBox()
        self._message_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._message_list.connect("row-selected", self._on_message_selected)
        message_scroll.set_child(self._message_list)

        message_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        message_panel.append(message_scroll)
        self._load_more_btn = Gtk.Button(label="Load more")
        self._load_more_btn.set_margin_start(12)
        self._load_more_btn.set_margin_end(12)
        self._load_more_btn.set_margin_top(6)
        self._load_more_btn.set_margin_bottom(6)
        self._load_more_btn.set_visible(False)
        self._load_more_btn.connect("clicked", self._on_load_more)
        message_panel.append(self._load_more_btn)

        self._message_stack.add_named(message_panel, "list")
        self._message_stack.set_visible_child_name("list")

        panes.append(self._message_stack)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep2)

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

    def begin_load(self) -> None:
        """Load accounts and folders after the window is on screen."""
        GLib.idle_add(self._reload_sidebar)

    def _set_status(self, text: str) -> None:
        self._status.set_label(text)

    def _prompt_account_password(
        self, account_label: str, _mechanism: str | None
    ) -> str | None:
        return prompt_password_sync(self, account_label)

    def _reload_sidebar(self) -> bool:
        self._clear_reader()
        self._clear_listbox(self._message_list)
        self._current_account = None
        self._current_folder = None
        self._sidebar.load()
        return False

    def _on_folder_selected(self, account: MailAccount, folder_name: str) -> None:
        self._current_account = account
        self._current_folder = folder_name
        self._load_messages(account.uid, folder_name)

    def _clear_reader(self) -> None:
        self._reader_subject.set_label("Select a message")
        self._reader_meta.set_label("")
        self._current_body = {"plain": None, "html": None}
        self._show_reader_document()

    def _on_refresh(self, *_args) -> None:
        if self._current_account and self._current_folder:
            self._load_messages(self._current_account.uid, self._current_folder)
        else:
            GLib.idle_add(self._reload_sidebar)

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        while child := listbox.get_first_child():
            listbox.remove(child)

    def _load_messages(
        self, account_uid: str, folder_name: str, *, offset: int = 0
    ) -> None:
        account = self._current_account
        if account is None or account.uid != account_uid:
            return

        if offset == 0:
            self._messages_load_generation += 1
            self._shown_message_count = 0
            self._message_total = -1
            self._message_has_more = False
            self._load_more_btn.set_visible(False)
            self._set_status(f"Loading {folder_name}…")
            self._clear_listbox(self._message_list)
            self._clear_reader()
            self._message_loading_label.set_label(f"Loading {folder_name}…")
            self._message_loading_spinner.start()
            self._message_stack.set_visible_child_name("loading")
        else:
            self._load_more_btn.set_sensitive(False)
            self._load_more_btn.set_label("Loading…")

        load_id = self._messages_load_generation

        def worker() -> None:
            error: Exception | None = None
            messages: list[dict] | None = None
            unread = -1
            total = -1
            has_more = False
            try:
                messages, unread, total, has_more = self._mail.list_messages_page(
                    account_uid,
                    folder_name,
                    offset=offset,
                    limit=DEFAULT_MESSAGE_PAGE_SIZE,
                )
            except Exception as exc:
                log.exception("Failed to list messages")
                error = exc
            GLib.idle_add(
                self._on_messages_loaded,
                load_id,
                account_uid,
                folder_name,
                offset,
                messages,
                unread,
                total,
                has_more,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_load_more(self, *_args) -> None:
        if (
            not self._current_account
            or not self._current_folder
            or not self._message_has_more
        ):
            return
        self._load_messages(
            self._current_account.uid,
            self._current_folder,
            offset=self._shown_message_count,
        )

    def _on_messages_loaded(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        offset: int,
        messages: list[dict] | None,
        unread: int,
        total: int,
        has_more: bool,
        error: Exception | None,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        self._message_loading_spinner.stop()
        self._load_more_btn.set_sensitive(True)
        self._load_more_btn.set_label("Load more")

        if error is not None:
            self._message_stack.set_visible_child_name("list")
            self._set_status(f"Message error: {error}")
            self._load_more_btn.set_visible(False)
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            self._message_stack.set_visible_child_name("list")
            return False

        if offset == 0:
            self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        self._message_total = total
        self._message_has_more = has_more
        self._message_stack.set_visible_child_name("list")
        self._populate_message_rows(
            load_id,
            messages,
            0,
            account,
            folder_name,
            page_offset=offset,
        )
        return False

    def _populate_message_rows(
        self,
        load_id: int,
        messages: list[dict],
        row_offset: int,
        account: MailAccount,
        folder_name: str,
        *,
        page_offset: int,
        batch_size: int = 25,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        end = min(row_offset + batch_size, len(messages))
        for msg in messages[row_offset:end]:
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
                load_id,
                messages,
                end,
                account,
                folder_name,
                page_offset=page_offset,
                batch_size=batch_size,
            )
            return False

        self._shown_message_count = page_offset + len(messages)
        self._load_more_btn.set_visible(self._message_has_more)
        self._update_message_status(account, folder_name)
        return False

    def _update_message_status(self, account: MailAccount, folder_name: str) -> None:
        shown = self._shown_message_count
        total = self._message_total
        label = account.display_label
        if total >= 0 and shown < total:
            self._set_status(f"Showing {shown} of {total} in {label} / {folder_name}")
        elif total >= 0:
            self._set_status(f"{total} messages in {label} / {folder_name}")
        else:
            self._set_status(f"{shown} messages in {label} / {folder_name}")

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
