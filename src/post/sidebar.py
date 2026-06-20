# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Left sidebar: accounts, folders, and unified Inbox section."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

gi.require_version("GObject", "2.0")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from post.mail import MailService
from post.mail.eds import MailAccount
from post.mail.folders import (
    filter_sidebar_folders,
    find_inbox_folder,
    format_folder_label,
    guess_inbox_name,
    resolve_move_menu_state,
)
from post.preferences import (
    get_sidebar_state,
    register_inbox_accounts,
    resolve_inbox_display_order,
    set_sidebar_state,
)

log = logging.getLogger(__name__)

OnFolderSelected = Callable[[MailAccount, str], None]
SetStatus = Callable[[str], None]
OnRefreshAccount = Callable[[str], None]
OnRefreshFolder = Callable[[str, str], None]
OnAccountsLoaded = Callable[[list[str]], None]


class MailSidebar:
    def __init__(
        self,
        mail: MailService,
        *,
        on_folder_selected: OnFolderSelected,
        set_status: SetStatus,
        on_refresh_account: OnRefreshAccount | None = None,
        on_refresh_folder: OnRefreshFolder | None = None,
        on_accounts_loaded: OnAccountsLoaded | None = None,
    ) -> None:
        self._mail = mail
        self._on_folder_selected = on_folder_selected
        self._set_status = set_status
        self._on_refresh_account = on_refresh_account
        self._on_refresh_folder = on_refresh_folder
        self._on_accounts_loaded = on_accounts_loaded

        self._accounts: list[MailAccount] = []
        self._accounts_by_uid: dict[str, MailAccount] = {}
        self._sidebar_selecting = False
        sidebar_state = get_sidebar_state()
        self._expanded_accounts: dict[str, bool] = dict(
            sidebar_state.get("accounts") or {}
        )
        self._inbox_expander: Gtk.Expander | None = None
        self._inbox_list: Gtk.ListBox | None = None
        self._inbox_expanded = bool(sidebar_state.get("inbox_expanded", True))
        self._saved_active_folder: tuple[str, str] | None = sidebar_state.get(
            "active_folder"
        )
        self._inbox_order: list[str] = list(sidebar_state.get("inbox_order") or [])
        self._folder_lists: dict[str, Gtk.ListBox] = {}
        self._account_folders: dict[str, list[dict]] = {}
        self._account_inbox_folders: dict[str, str] = {}
        self._load_generation = 0
        self._needs_initial_selection = False
        self._activated_folder: tuple[str, str] | None = None
        self._refresh_target: tuple[str, str | None] | None = None

        self._sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._sidebar_box.add_css_class("navigation-sidebar")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(240, -1)
        scroll.set_child(self._sidebar_box)
        self._widget = scroll
        self._setup_refresh_menu()

    @property
    def widget(self) -> Gtk.ScrolledWindow:
        return self._widget

    def load(self) -> None:
        self._persist_view_state()
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
                "No mail accounts found. Add one in Settings → Online Accounts, "
                "or configure local mail in Post Settings."
            )
            return

        self._accounts_by_uid = {a.uid: a for a in self._accounts}

        if self._on_accounts_loaded is not None:
            self._on_accounts_loaded([account.uid for account in self._accounts])

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
        inbox_name = self._inbox_folder_name(account_uid)
        if not inbox_name:
            return

        def worker() -> None:
            error: Exception | None = None
            unread = -1
            total = -1
            try:
                unread, total = self._mail.get_folder_stats(account_uid, inbox_name)
            except Exception as exc:
                log.exception("Failed to refresh inbox counts for %s", account_uid)
                error = exc
            GLib.idle_add(
                self._on_inbox_counts_refreshed,
                account_uid,
                inbox_name,
                unread,
                total,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _inbox_folder_name(self, account_uid: str) -> str | None:
        inbox_name = self._account_inbox_folders.get(account_uid)
        if inbox_name:
            return inbox_name
        inbox = find_inbox_folder(self._account_folders.get(account_uid, []))
        if inbox is None:
            return None
        return inbox.get("full_name")

    def _on_inbox_counts_refreshed(
        self,
        account_uid: str,
        folder_name: str,
        unread: int,
        total: int,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            return False

        self._account_inbox_folders[account_uid] = folder_name
        self.update_folder_row(account_uid, folder_name, unread, total)
        return False

    def reload_account(self, account_uid: str) -> None:
        account = self._accounts_by_uid.get(account_uid)
        folder_list = self._folder_lists.get(account_uid)
        if account is None or folder_list is None:
            return

        self._clear_listbox(folder_list)
        loading = Gtk.Label(label="Loading folders…", xalign=0)
        loading.add_css_class("dim-label")
        loading.set_margin_start(12)
        loading.set_margin_end(12)
        loading.set_margin_bottom(8)
        folder_list.append(self._wrap_list_row(loading))
        self._start_folder_load(self._load_generation, account)

    def refresh_folder_row(self, account_uid: str, folder_name: str) -> None:
        def worker() -> None:
            error: Exception | None = None
            unread = -1
            total = -1
            try:
                unread, total = self._mail.get_folder_stats(account_uid, folder_name)
            except Exception as exc:
                log.exception(
                    "Failed to refresh folder %s/%s", account_uid, folder_name
                )
                error = exc
            GLib.idle_add(
                self._on_folder_row_refreshed,
                account_uid,
                folder_name,
                unread,
                total,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_folder_row_refreshed(
        self,
        account_uid: str,
        folder_name: str,
        unread: int,
        total: int,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            return False
        self.update_folder_row(account_uid, folder_name, unread, total)
        return False

    def _setup_refresh_menu(self) -> None:
        action = Gio.SimpleAction.new("refresh", None)
        action.connect("activate", self._on_refresh_menu_activate)
        group = Gio.SimpleActionGroup.new()
        group.add_action(action)
        self._widget.insert_action_group("sidebar", group)

        menu = Gio.Menu()
        menu.append("Refresh", "sidebar.refresh")
        self._refresh_popover = Gtk.PopoverMenu.new_from_model(menu)

    def _on_refresh_menu_activate(self, *_args) -> None:
        if self._refresh_target is None:
            return
        account_uid, folder_name = self._refresh_target
        self._refresh_target = None
        if folder_name is None:
            if self._on_refresh_account is not None:
                self._on_refresh_account(account_uid)
            return
        if self._on_refresh_folder is not None:
            self._on_refresh_folder(account_uid, folder_name)

    def _attach_refresh_menu(
        self,
        widget: Gtk.Widget,
        *,
        account_uid: str,
        folder_name: str | None,
    ) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(0)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect(
            "pressed",
            self._on_sidebar_context_pressed,
            account_uid,
            folder_name,
        )
        widget.add_controller(gesture)

    def _on_sidebar_context_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
        account_uid: str,
        folder_name: str | None,
    ) -> None:
        if n_press != 1:
            return
        event = gesture.get_current_event()
        if event is None or not Gdk.Event.triggers_context_menu(event):
            return

        self._refresh_target = (account_uid, folder_name)
        widget = gesture.get_widget()
        if widget is None:
            return
        self._ensure_refresh_popover_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._refresh_popover.set_pointing_to(rect)
        self._refresh_popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _ensure_refresh_popover_parent(self, widget: Gtk.Widget) -> None:
        current = self._refresh_popover.get_parent()
        if current is widget:
            return
        if current is not None:
            self._refresh_popover.popdown()
            if self._refresh_popover.get_parent() is current:
                self._refresh_popover.unparent()
        self._refresh_popover.set_parent(widget)

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
        folders = filter_sidebar_folders(folders)
        self._account_folders[account_uid] = folders
        for folder in folders:
            folder_list.append(self._make_folder_row(account_uid, folder))

        self._add_inbox_row(account_uid, folders)
        self.refresh_inbox_counts(account_uid)

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

    def _persist_view_state(self) -> None:
        self._save_expanded_state()
        set_sidebar_state(
            inbox_expanded=self._inbox_expanded,
            accounts=self._expanded_accounts,
            active_folder=self._saved_active_folder,
            inbox_order=self._inbox_order,
        )

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
        saved = self._saved_active_folder
        if saved is not None:
            account_uid, folder_name = saved
            for listbox in self._all_folder_listboxes():
                row = listbox.get_first_child()
                while row is not None:
                    if (
                        getattr(row, "account_uid", None) == account_uid
                        and getattr(row, "folder_name", None) == folder_name
                    ):
                        return listbox, row
                    row = row.get_next_sibling()

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
        self._setup_inbox_list_dnd(inbox_list)
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
        self._persist_view_state()

    def _add_inbox_row(self, account_uid: str, folders: list[dict]) -> None:
        if self._inbox_list is None:
            return

        inbox_folder = find_inbox_folder(folders)
        if inbox_folder is None:
            return

        row = self._inbox_list.get_first_child()
        while row is not None:
            next_row = row.get_next_sibling()
            if getattr(row, "account_uid", None) == account_uid or (
                getattr(row, "folder_name", None) is None
            ):
                self._inbox_list.remove(row)
            row = next_row

        account = self._accounts_by_uid.get(account_uid)
        display = account.display_label if account else account_uid
        full_name = inbox_folder.get("full_name")
        if full_name:
            self._account_inbox_folders[account_uid] = full_name
        self._inbox_list.append(
            self._make_folder_row(account_uid, inbox_folder, display=display)
        )
        row = self._inbox_list.get_last_child()
        if isinstance(row, Gtk.ListBoxRow):
            self._setup_inbox_row_drag(row)
        self._sort_inbox_list()

    def _current_inbox_order_from_list(self) -> list[str]:
        if self._inbox_list is None:
            return list(self._inbox_order)
        order: list[str] = []
        row = self._inbox_list.get_first_child()
        while row is not None:
            uid = getattr(row, "account_uid", None)
            if uid:
                order.append(uid)
            row = row.get_next_sibling()
        return order

    def _resolve_inbox_order(self, present: list[str]) -> list[str]:
        return resolve_inbox_display_order(self._inbox_order, present)

    def _register_inbox_accounts(self, present: list[str]) -> None:
        self._inbox_order = register_inbox_accounts(self._inbox_order, present)

    def _sort_inbox_list(self) -> None:
        if self._inbox_list is None:
            return

        placeholders: list[Gtk.ListBoxRow] = []
        rows_by_uid: dict[str, Gtk.ListBoxRow] = {}
        row = self._inbox_list.get_first_child()
        while row is not None:
            next_row = row.get_next_sibling()
            uid = getattr(row, "account_uid", None)
            self._inbox_list.remove(row)
            if uid:
                rows_by_uid[uid] = row
            else:
                placeholders.append(row)
            row = next_row

        for placeholder in placeholders:
            self._inbox_list.append(placeholder)

        present = list(rows_by_uid.keys())
        self._register_inbox_accounts(present)
        order = self._resolve_inbox_order(present)
        for uid in order:
            self._inbox_list.append(rows_by_uid[uid])

    def _move_inbox_row(self, source_uid: str, target_uid: str, *, after: bool) -> None:
        order = self._current_inbox_order_from_list()
        if source_uid not in order or target_uid not in order:
            return
        order.remove(source_uid)
        index = order.index(target_uid)
        if after:
            index += 1
        order.insert(index, source_uid)
        self._inbox_order = order
        self._sort_inbox_list()
        self._persist_view_state()

    def _setup_inbox_row_drag(self, row: Gtk.ListBoxRow) -> None:
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)

        def prepare(_source: Gtk.DragSource, _x: float, _y: float) -> Gdk.ContentProvider | None:
            account_uid = getattr(row, "account_uid", None)
            if not account_uid:
                return None
            return Gdk.ContentProvider.new_for_value(account_uid)

        drag_source.connect("prepare", prepare)
        row.add_controller(drag_source)

    def _setup_inbox_list_dnd(self, inbox_list: Gtk.ListBox) -> None:
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def drop(
            _target: Gtk.DropTarget,
            value: object,
            x: float,
            y: float,
        ) -> bool:
            source_uid = value if isinstance(value, str) else None
            if not source_uid:
                return False

            target_row = inbox_list.get_row_at_y(int(y))
            if target_row is None:
                order = self._current_inbox_order_from_list()
                if source_uid not in order:
                    return False
                order.remove(source_uid)
                order.append(source_uid)
                self._inbox_order = order
                self._sort_inbox_list()
                self._persist_view_state()
                return True

            target_uid = getattr(target_row, "account_uid", None)
            if not target_uid or target_uid == source_uid:
                return False

            allocation = target_row.get_allocation()
            after = y > allocation.y + allocation.height / 2
            self._move_inbox_row(source_uid, target_uid, after=after)
            return True

        drop_target.connect("drop", drop)
        inbox_list.add_controller(drop_target)

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
        self._persist_view_state()

    def _make_account_header(self, account: MailAccount) -> Gtk.Widget:
        label = Gtk.Label(label=account.display_label, xalign=0)
        label.add_css_class("heading")
        label.set_ellipsize(3)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        self._attach_refresh_menu(label, account_uid=account.uid, folder_name=None)
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
        if row.folder_name:
            self._attach_refresh_menu(
                row,
                account_uid=account_uid,
                folder_name=row.folder_name,
            )
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
        self._saved_active_folder = selection
        self._persist_view_state()
        self._on_folder_selected(account, folder_name)
