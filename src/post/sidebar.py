# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Left sidebar: accounts, folders, and unified Inbox section."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

from post.mail import MailService
from post.mail.eds import MailAccount
from post.mail.folders import (
    find_inbox_folder,
    format_folder_label,
    guess_inbox_name,
    resolve_move_menu_state,
)

log = logging.getLogger(__name__)

OnFolderSelected = Callable[[MailAccount, str], None]
SetStatus = Callable[[str], None]


class MailSidebar:
    def __init__(
        self,
        mail: MailService,
        *,
        on_folder_selected: OnFolderSelected,
        set_status: SetStatus,
    ) -> None:
        self._mail = mail
        self._on_folder_selected = on_folder_selected
        self._set_status = set_status

        self._accounts: list[MailAccount] = []
        self._accounts_by_uid: dict[str, MailAccount] = {}
        self._sidebar_selecting = False
        self._expanded_accounts: dict[str, bool] = {}
        self._inbox_expander: Gtk.Expander | None = None
        self._inbox_list: Gtk.ListBox | None = None
        self._inbox_expanded = True
        self._folder_lists: dict[str, Gtk.ListBox] = {}
        self._account_folders: dict[str, list[dict]] = {}
        self._account_inbox_folders: dict[str, str] = {}
        self._load_generation = 0
        self._needs_initial_selection = False
        self._activated_folder: tuple[str, str] | None = None

        self._sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._sidebar_box.add_css_class("navigation-sidebar")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(240, -1)
        scroll.set_child(self._sidebar_box)
        self._widget = scroll

    @property
    def widget(self) -> Gtk.ScrolledWindow:
        return self._widget

    def load(self) -> None:
        self._save_expanded_state()
        self._clear()
        self._load_generation += 1
        load_id = self._load_generation
        self._needs_initial_selection = True
        self._activated_folder = None

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

        if len(self._accounts) > 1:
            self._sidebar_box.append(self._make_inbox_section_loading())

        for account in self._accounts:
            self._sidebar_box.append(self._make_account_section_loading(account))
            self._start_folder_load(load_id, account)

        self._set_status(f"{len(self._accounts)} account(s)")

    def update_folder_row(
        self, account_uid: str, folder_name: str, unread: int, total: int
    ) -> None:
        for folder_list in self._all_folder_listboxes():
            row = folder_list.get_first_child()
            while row is not None:
                if (
                    getattr(row, "account_uid", None) == account_uid
                    and getattr(row, "folder_name", None) == folder_name
                ):
                    label = row.get_child()
                    if isinstance(label, Gtk.Label):
                        display = getattr(row, "display_name", folder_name)
                        label.set_label(format_folder_label(display, unread, total))
                row = row.get_next_sibling()

    def get_move_menu_state(self, account_uid: str, folder_name: str) -> dict:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        folders = self._account_folders.get(account_uid, [])
        return resolve_move_menu_state(
            folders,
            folder_name,
            archive_type=Camel.FolderInfoFlags.TYPE_ARCHIVE,
            trash_type=Camel.FolderInfoFlags.TYPE_TRASH,
            type_mask=Camel.FOLDER_TYPE_MASK,
        )

    def inbox_folder_for_account(self, account_uid: str) -> str | None:
        return self._account_inbox_folders.get(account_uid)

    def refresh_inbox_counts(self, account_uid: str) -> None:
        """Re-fetch inbox stats and update sidebar rows (incl. unified Inbox)."""

        def worker() -> None:
            error: Exception | None = None
            inbox: dict | None = None
            try:
                folders = self._mail.list_folders(account_uid)
                inbox = find_inbox_folder(folders)
            except Exception as exc:
                log.exception("Failed to refresh inbox counts for %s", account_uid)
                error = exc
            GLib.idle_add(
                self._on_inbox_counts_refreshed,
                account_uid,
                inbox,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_inbox_counts_refreshed(
        self,
        account_uid: str,
        inbox: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None or inbox is None:
            return False

        folder_name = inbox.get("full_name")
        if not folder_name:
            return False

        self._account_inbox_folders[account_uid] = folder_name
        unread = inbox.get("unread", -1)
        total = inbox.get("total", -1)
        self.update_folder_row(account_uid, folder_name, unread, total)
        return False

    def _start_folder_load(self, load_id: int, account: MailAccount) -> None:
        def worker() -> None:
            error: Exception | None = None
            folders: list[dict] | None = None
            try:
                folders = self._mail.list_folders(account.uid)
            except Exception as exc:
                log.exception("Failed to list folders for %s", account.uid)
                error = exc
            GLib.idle_add(
                self._on_folders_loaded,
                load_id,
                account.uid,
                folders,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_folders_loaded(
        self,
        load_id: int,
        account_uid: str,
        folders: list[dict] | None,
        error: Exception | None,
    ) -> bool:
        if load_id != self._load_generation:
            return False

        folder_list = self._folder_lists.get(account_uid)
        if folder_list is None:
            return False

        self._clear_listbox(folder_list)

        if error is not None:
            error_label = Gtk.Label(
                label=f"Could not load folders: {error}",
                xalign=0,
                wrap=True,
            )
            error_label.add_css_class("dim-label")
            error_label.set_margin_start(12)
            error_label.set_margin_end(12)
            error_label.set_margin_bottom(8)
            folder_list.append(self._wrap_list_row(error_label))
            return False

        assert folders is not None
        self._account_folders[account_uid] = folders
        for folder in folders:
            folder_list.append(self._make_folder_row(account_uid, folder))

        self._add_inbox_row(account_uid, folders)

        if self._needs_initial_selection:
            initial_list, initial_row = self._find_initial_folder()
            if initial_list is not None and initial_row is not None:
                self._activate_folder_row(initial_list, initial_row)
                self._needs_initial_selection = False

        return False

    def _save_expanded_state(self) -> None:
        if self._inbox_expander is not None:
            self._inbox_expanded = self._inbox_expander.get_expanded()
        for uid, listbox in self._folder_lists.items():
            expander = listbox.get_parent()
            while expander is not None and not isinstance(expander, Gtk.Expander):
                expander = expander.get_parent()
            if isinstance(expander, Gtk.Expander):
                self._expanded_accounts[uid] = expander.get_expanded()

    def _clear(self) -> None:
        while child := self._sidebar_box.get_first_child():
            self._sidebar_box.remove(child)
        self._folder_lists.clear()
        self._account_folders.clear()
        self._account_inbox_folders.clear()
        self._inbox_expander = None
        self._inbox_list = None

    def _all_folder_listboxes(self) -> list[Gtk.ListBox]:
        lists = list(self._folder_lists.values())
        if self._inbox_list is not None:
            lists.append(self._inbox_list)
        return lists

    def _find_initial_folder(self) -> tuple[Gtk.ListBox | None, Gtk.ListBoxRow | None]:
        if self._inbox_list is not None:
            row = self._inbox_list.get_first_child()
            while row is not None:
                if getattr(row, "folder_name", None):
                    return self._inbox_list, row
                row = row.get_next_sibling()

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

    def _make_inbox_section_loading(self) -> Gtk.Expander:
        expander = Gtk.Expander()
        expander.set_expanded(self._inbox_expanded)
        expander.connect("notify::expanded", self._on_inbox_expanded)
        header = Gtk.Label(label="Inbox", xalign=0)
        header.add_css_class("heading")
        header.set_margin_top(4)
        header.set_margin_bottom(4)
        expander.set_label_widget(header)
        expander.set_margin_start(6)
        expander.set_margin_end(6)
        expander.set_margin_top(4)

        inbox_list = Gtk.ListBox()
        inbox_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        inbox_list.add_css_class("navigation-sidebar")
        inbox_list.connect("row-selected", self._on_folder_row_selected)
        self._inbox_list = inbox_list
        self._inbox_expander = expander

        loading = Gtk.Label(label="Loading inboxes…", xalign=0)
        loading.add_css_class("dim-label")
        loading.set_margin_start(12)
        loading.set_margin_end(12)
        loading.set_margin_bottom(8)
        inbox_list.append(self._wrap_list_row(loading))

        expander.set_child(inbox_list)
        return expander

    def _on_inbox_expanded(self, expander: Gtk.Expander, _pspec) -> None:
        self._inbox_expanded = expander.get_expanded()

    def _add_inbox_row(self, account_uid: str, folders: list[dict]) -> None:
        if self._inbox_list is None:
            return

        inbox_folder = find_inbox_folder(folders)
        if inbox_folder is None:
            return

        first = self._inbox_list.get_first_child()
        if first is not None and getattr(first, "folder_name", None) is None:
            self._inbox_list.remove(first)

        account = self._accounts_by_uid.get(account_uid)
        display = account.display_label if account else account_uid
        full_name = inbox_folder.get("full_name")
        if full_name:
            self._account_inbox_folders[account_uid] = full_name
        self._inbox_list.append(
            self._make_folder_row(account_uid, inbox_folder, display=display)
        )

    def _make_account_section_loading(self, account: MailAccount) -> Gtk.Expander:
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
        folder_list.connect("row-selected", self._on_folder_row_selected)
        self._folder_lists[account.uid] = folder_list

        loading = Gtk.Label(label="Loading folders…", xalign=0)
        loading.add_css_class("dim-label")
        loading.set_margin_start(12)
        loading.set_margin_end(12)
        loading.set_margin_bottom(8)
        folder_list.append(self._wrap_list_row(loading))

        expander.set_child(folder_list)
        return expander

    def _on_account_expanded(self, expander: Gtk.Expander, _pspec, account_uid: str) -> None:
        self._expanded_accounts[account_uid] = expander.get_expanded()

    def _make_account_header(self, account: MailAccount) -> Gtk.Widget:
        label = Gtk.Label(label=account.display_label, xalign=0)
        label.add_css_class("heading")
        label.set_ellipsize(3)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        return label

    @staticmethod
    def _wrap_list_row(widget: Gtk.Widget) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_child(widget)
        return row

    def _make_folder_row(
        self,
        account_uid: str,
        folder: dict,
        *,
        display: str | None = None,
    ) -> Gtk.ListBoxRow:
        if display is None:
            display = folder.get("display_name") or folder.get("full_name") or "?"
        unread = folder.get("unread", -1)
        total = folder.get("total", -1)
        label_text = format_folder_label(display, unread, total)

        label = Gtk.Label(label=label_text, xalign=0, margin_start=12, margin_end=12)
        row = Gtk.ListBoxRow()
        row.set_child(label)
        row.account_uid = account_uid
        row.folder_name = folder.get("full_name")
        row.display_name = display
        return row

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        while child := listbox.get_first_child():
            listbox.remove(child)

    def _on_folder_row_selected(
        self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None or self._sidebar_selecting:
            return
        self._activate_folder_row(listbox, row)

    def _activate_folder_row(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        account_uid = getattr(row, "account_uid", None)
        folder_name = getattr(row, "folder_name", None)
        if not account_uid or not folder_name:
            return

        selection = (account_uid, folder_name)
        if selection == self._activated_folder:
            return

        for other in self._all_folder_listboxes():
            if other is not listbox:
                other.unselect_all()

        self._sidebar_selecting = True
        listbox.select_row(row)
        self._sidebar_selecting = False

        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            return

        self._activated_folder = selection
        self._on_folder_selected(account, folder_name)
