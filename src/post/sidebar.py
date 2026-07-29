# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Left sidebar: accounts, folders, and unified Inbox section."""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

gi.require_version("GObject", "2.0")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from post.mail import MailService
from post.mail import folder_status_cache
from post.mail.eds import MailAccount
from post.mail.io_thread import get_mail_io_thread
from post.folder_dialogs import confirm_action, prompt_folder_name, show_error
from post.gtk_schedule import schedule_on_gtk_main
from post.mail.dnd import (
    MESSAGE_TRANSFER_MIME,
    decode_message_transfer,
    validate_message_drop,
)
from post.mail.folders import (
    POST_OUTBOX_FOLDER,
    account_supports_folder_crud,
    filter_sidebar_folders,
    find_inbox_folder,
    folder_names_for_count_refresh,
    format_folder_label,
    format_startup_loading_folders,
    is_drafts_folder_name,
    is_sent_folder_name,
    is_post_outbox_folder,
    outbox_folder_dict,
    resolve_folder_display_name,
    resolve_move_menu_state,
    resolve_sidebar_context_menu,
)
from post.mail.message_list_state import is_heavy_folder_name
from post.mail.offline_settings import account_is_user_offline
from post.mail.send_queue import (
    count_queued_for_account,
    format_folder_load_error,
    is_network_unavailable_error,
    is_sign_in_required_error,
    log_mail_error,
)
from post.mail.account_status import account_not_online_badge
from post.preferences import (
    account_supports_user_offline,
    get_account_user_online,
    get_sidebar_state,
    register_inbox_accounts,
    resolve_inbox_display_order,
    set_sidebar_state,
)

log = logging.getLogger(__name__)


def _flush_log_handlers() -> None:
    """Flush handlers so the last DEBUG line survives a native Gtk abort."""
    for handler in logging.root.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _debug_listbox_start(op: str, **fields: object) -> None:
    """Log entry into a ListBox mutation block (#201 crash isolation)."""
    extras = " ".join(f"{key}={value!r}" for key, value in fields.items())
    log.debug("start %s %s", op, extras)
    _flush_log_handlers()


def _debug_listbox_end(op: str, **fields: object) -> None:
    """Log exit from a ListBox mutation block; missing end narrows crash site."""
    extras = " ".join(f"{key}={value!r}" for key, value in fields.items())
    log.debug("end %s %s", op, extras)
    _flush_log_handlers()


OnFolderSelected = Callable[[MailAccount, str], None]
SetStatus = Callable[[str], None]
OnRefreshAccount = Callable[[str], None]
OnRefreshFolder = Callable[[str, str], None]
OnAccountsLoaded = Callable[[list[str]], None]
OnInitialFolderLoadComplete = Callable[[], None]
OnFolderTreeReady = Callable[[], None]
OnSendOutbox = Callable[[], None]
OnFolderTreeChanged = Callable[[str, str | None], None]
OnFolderContentsChanged = Callable[[str, str], None]
OnMoveStarted = Callable[[str, str], None]
OnMoveUndoAvailable = Callable[[str, str, dict, str], None]
OnAccountOnlineChanged = Callable[[str, bool], None]
OnGoaReauthRequested = Callable[[str], None]
OnMessagesDropped = Callable[[str, str, str, list[str]], None]
FolderRefreshComplete = Callable[[int, int, Exception | None], None]
AccountRefreshComplete = Callable[[int, Exception | None], None]


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
        on_initial_folder_load_complete: OnInitialFolderLoadComplete | None = None,
        on_folder_tree_ready: OnFolderTreeReady | None = None,
        on_send_outbox: OnSendOutbox | None = None,
        on_folder_tree_changed: OnFolderTreeChanged | None = None,
        on_folder_contents_changed: OnFolderContentsChanged | None = None,
        on_move_started: OnMoveStarted | None = None,
        on_move_undo_available: OnMoveUndoAvailable | None = None,
        on_account_online_changed: OnAccountOnlineChanged | None = None,
        on_goa_reauth_requested: OnGoaReauthRequested | None = None,
        on_messages_dropped: OnMessagesDropped | None = None,
    ) -> None:
        self._mail = mail
        self._on_folder_selected = on_folder_selected
        self._set_status = set_status
        self._on_refresh_account = on_refresh_account
        self._on_refresh_folder = on_refresh_folder
        self._on_accounts_loaded = on_accounts_loaded
        self._on_initial_folder_load_complete = on_initial_folder_load_complete
        self._on_folder_tree_ready = on_folder_tree_ready
        self._on_send_outbox = on_send_outbox
        self._on_folder_tree_changed = on_folder_tree_changed
        self._on_folder_contents_changed = on_folder_contents_changed
        self._on_move_started = on_move_started
        self._on_move_undo_available = on_move_undo_available
        self._on_account_online_changed = on_account_online_changed
        self._on_goa_reauth_requested = on_goa_reauth_requested
        self._on_messages_dropped = on_messages_dropped
        self._network_available = True
        self._account_offline_icons: dict[str, Gtk.Image] = {}
        self._inbox_offline_icons: dict[str, Gtk.Image] = {}

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
        self._folder_loads_pending = 0
        self._startup_folder_total = 0
        # False until a folder-list load cycle has finished at least once.
        # Pending==0 alone is not enough: before load() starts (and during the
        # gap before pending is bumped), pending is also 0.
        self._folder_tree_ready = False
        self._needs_initial_selection = False
        self._activated_folder: tuple[str, str] | None = None
        self._context_target: dict | None = None
        self._context_actions: dict[str, Gio.SimpleAction] = {}
        self._context_popover: Gtk.PopoverMenu | None = None
        self._account_reload_callbacks: dict[str, AccountRefreshComplete] = {}
        self._folder_count_poll_generation = 0
        # Heavy folders awaiting first trusted STATUS poll (#208).
        self._heavy_status_pending: set[tuple[str, str]] = set()

        self._sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(240, -1)
        scroll.set_child(self._sidebar_box)
        self._widget = scroll
        self._setup_context_menu()

    @property
    def widget(self) -> Gtk.ScrolledWindow:
        return self._widget

    @property
    def folder_tree_ready(self) -> bool:
        return self._folder_tree_ready

    def account_uids(self) -> list[str]:
        return [account.uid for account in self._accounts]

    def load(self) -> None:
        self._persist_view_state()
        self._clear()
        self._load_generation += 1
        load_id = self._load_generation
        self._needs_initial_selection = True
        self._activated_folder = None
        self._folder_loads_pending = 0
        self._startup_folder_total = 0
        self._folder_tree_ready = False

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
            # Seed Inboxes immediately so degraded accounts (expired GOA, etc.)
            # stay visible even if folder-list fails or is cancelled/retried.
            for account in self._accounts:
                self._add_inbox_row_unavailable(account.uid)

        self._folder_loads_pending = len(self._accounts)
        self._startup_folder_total = len(self._accounts)
        for account in self._accounts:
            self._sidebar_box.append(self._make_account_section_loading(account))
            self._start_folder_load(load_id, account)

        self._set_status(
            format_startup_loading_folders(0, self._startup_folder_total)
        )

    def update_folder_row(
        self,
        account_uid: str,
        folder_name: str,
        unread: int,
        total: int,
        *,
        status_trusted: bool = False,
    ) -> None:
        if is_heavy_folder_name(folder_name):
            # Callers may pass Camel summary sizes; never show those as STATUS.
            folder_status_cache.observe(
                account_uid,
                folder_name,
                unread,
                total,
                trusted=status_trusted,
            )
            unread, total = folder_status_cache.resolve_sidebar(
                account_uid, folder_name, unread, total
            )
            key = (account_uid, folder_name)
            if total >= 0:
                self._heavy_status_pending.discard(key)
            elif (
                self._network_available
                and not account_is_user_offline(account_uid)
                and folder_status_cache.load(account_uid, folder_name) is None
            ):
                self._heavy_status_pending.add(key)

        pending = (account_uid, folder_name) in self._heavy_status_pending
        for folder_list in self._all_folder_listboxes():
            row = folder_list.get_first_child()
            while row is not None:
                if (
                    getattr(row, "account_uid", None) == account_uid
                    and getattr(row, "folder_name", None) == folder_name
                ):
                    row.unread = unread
                    row.total = total
                    label = self._folder_row_label(row)
                    if label is not None:
                        display = getattr(row, "display_name", folder_name)
                        label.set_label(
                            format_folder_label(
                                display,
                                unread,
                                total,
                                status_pending=pending,
                            )
                        )
                row = row.get_next_sibling()

    @staticmethod
    def _folder_row_label(row: Gtk.ListBoxRow) -> Gtk.Label | None:
        child = row.get_child()
        if isinstance(child, Gtk.Label):
            return child
        if isinstance(child, Gtk.Box):
            widget = child.get_first_child()
            while widget is not None:
                if isinstance(widget, Gtk.Label):
                    return widget
                widget = widget.get_next_sibling()
        return None

    def get_move_menu_state(self, account_uid: str, folder_name: str) -> dict:
        if is_post_outbox_folder(folder_name):
            return {
                "archive_folder": None,
                "trash_folder": None,
                "inbox_folder": self.inbox_folder_for_account(account_uid),
                "can_archive": False,
                "can_trash": True,
            }
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

    def set_network_available(self, available: bool) -> None:
        self._network_available = available
        self.refresh_all_account_online_markers()

    def refresh_outbox_row(self, account_uid: str) -> None:
        count = count_queued_for_account(account_uid)
        self.update_folder_row(account_uid, POST_OUTBOX_FOLDER, 0, count)

    def refresh_outbox_rows(self) -> None:
        for account_uid in self._folder_lists:
            self.refresh_outbox_row(account_uid)

    def inbox_folder_for_account(self, account_uid: str) -> str | None:
        return self._account_inbox_folders.get(account_uid)

    def account_display_label(self, account_uid: str) -> str:
        account = self._accounts_by_uid.get(account_uid)
        return account.display_label if account else account_uid

    def folder_display_name(self, account_uid: str, folder_name: str) -> str:
        display_name: str | None = None
        for folder in self._account_folders.get(account_uid, []):
            if folder.get("full_name") == folder_name:
                display_name = folder.get("display_name")
                break
        return resolve_folder_display_name(
            folder_name=folder_name,
            display_name=display_name,
            inbox_name=self.inbox_folder_for_account(account_uid),
            account_label=self.account_display_label(account_uid),
            is_outbox=is_post_outbox_folder(folder_name),
        )

    def folder_is_drafts(self, account_uid: str, folder_name: str) -> bool:
        return is_drafts_folder_name(
            self._account_folders.get(account_uid, []),
            folder_name,
        )

    def folder_is_sent(self, account_uid: str, folder_name: str) -> bool:
        return is_sent_folder_name(
            self._account_folders.get(account_uid, []),
            folder_name,
        )

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
                log_mail_error(log, f"Failed to refresh inbox counts for {account_uid}", exc)
                error = exc
            GLib.idle_add(
                self._on_inbox_counts_refreshed,
                account_uid,
                inbox_name,
                unread,
                total,
                error,
            )

        get_mail_io_thread().submit(worker)

    def refresh_all_folder_counts(self) -> None:
        """Re-fetch unread/total for all sidebar message folders (badge-only).

        Uses store-level folder-info REFRESH per account (STATUS-style), not
        per-folder refresh_info_sync, so Camel Folder::changed is not stormed.
        """
        account_uids = [
            account_uid
            for account_uid in self._account_folders
            if not account_is_user_offline(account_uid)
            and folder_names_for_count_refresh(
                self._account_folders.get(account_uid, [])
            )
        ]
        if not account_uids:
            return

        self._folder_count_poll_generation += 1
        generation = self._folder_count_poll_generation
        self._poll_account_folder_counts_at(generation, account_uids, 0)

    def cancel_folder_count_poll(self) -> None:
        """Invalidate any in-flight background folder-count poll."""
        self._folder_count_poll_generation += 1

    def _poll_account_folder_counts_at(
        self,
        generation: int,
        account_uids: list[str],
        index: int,
    ) -> None:
        if generation != self._folder_count_poll_generation:
            return
        if index >= len(account_uids):
            return

        account_uid = account_uids[index]

        def worker() -> None:
            if generation != self._folder_count_poll_generation:
                return
            if get_mail_io_thread().has_interactive_work_pending():
                schedule_on_gtk_main(
                    self._defer_account_folder_count_poll,
                    generation,
                    account_uids,
                    index,
                )
                return

            stats: dict[str, tuple[int, int]] = {}
            error: Exception | None = None
            try:
                stats = self._mail.get_account_folder_stats(account_uid)
            except Exception as exc:
                log_mail_error(
                    log,
                    f"Failed to refresh folder counts for {account_uid}",
                    exc,
                )
                error = exc
            schedule_on_gtk_main(
                self._on_account_folder_counts_polled,
                generation,
                account_uids,
                index,
                account_uid,
                stats,
                error,
            )

        get_mail_io_thread().submit_background(worker)

    def _defer_account_folder_count_poll(
        self,
        generation: int,
        account_uids: list[str],
        index: int,
    ) -> None:
        if generation != self._folder_count_poll_generation:
            return
        GLib.timeout_add(
            250,
            self._resume_account_folder_count_poll,
            generation,
            account_uids,
            index,
        )

    def _resume_account_folder_count_poll(
        self,
        generation: int,
        account_uids: list[str],
        index: int,
    ) -> bool:
        self._poll_account_folder_counts_at(generation, account_uids, index)
        return False

    def _on_account_folder_counts_polled(
        self,
        generation: int,
        account_uids: list[str],
        index: int,
        account_uid: str,
        stats: dict[str, tuple[int, int]],
        error: Exception | None,
    ) -> None:
        if generation != self._folder_count_poll_generation:
            return
        if error is None:
            wanted = set(
                folder_names_for_count_refresh(
                    self._account_folders.get(account_uid, [])
                )
            )
            for folder_name, (unread, total) in stats.items():
                if folder_name not in wanted:
                    continue
                if unread < 0 and total < 0:
                    # Clear poisoned summary-sized badges (show name only).
                    if not is_heavy_folder_name(folder_name):
                        continue
                self.update_folder_row(
                    account_uid,
                    folder_name,
                    unread,
                    total,
                    status_trusted=True,
                )
        # STATUS poll for this account finished (success or error) — stop
        # "working…" even if we still lack a trusted total.
        self._clear_heavy_status_pending_for_account(account_uid)
        self._poll_account_folder_counts_at(generation, account_uids, index + 1)

    def _clear_heavy_status_pending_for_account(self, account_uid: str) -> None:
        pending = [
            folder_name
            for pending_uid, folder_name in self._heavy_status_pending
            if pending_uid == account_uid
        ]
        for folder_name in pending:
            self._heavy_status_pending.discard((account_uid, folder_name))
            # Refresh label: drop "(working…)" if counts are still unknown.
            for folder_list in self._all_folder_listboxes():
                row = folder_list.get_first_child()
                while row is not None:
                    if (
                        getattr(row, "account_uid", None) == account_uid
                        and getattr(row, "folder_name", None) == folder_name
                    ):
                        unread = int(getattr(row, "unread", -1))
                        total = int(getattr(row, "total", -1))
                        label = self._folder_row_label(row)
                        if label is not None:
                            display = getattr(row, "display_name", folder_name)
                            label.set_label(
                                format_folder_label(display, unread, total)
                            )
                    row = row.get_next_sibling()

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

    def refresh_folder_counts(self, account_uid: str, folder_name: str) -> None:
        """Re-fetch folder stats and update sidebar rows."""

        def worker() -> None:
            error: Exception | None = None
            unread = -1
            total = -1
            try:
                unread, total = self._mail.get_folder_stats(account_uid, folder_name)
            except Exception as exc:
                log_mail_error(
                    log,
                    f"Failed to refresh counts for {folder_name!r} ({account_uid})",
                    exc,
                )
                error = exc
            GLib.idle_add(
                self._on_folder_counts_refreshed,
                account_uid,
                folder_name,
                unread,
                total,
                error,
            )

        get_mail_io_thread().submit(worker)

    def _on_folder_counts_refreshed(
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

    def reload_account(
        self,
        account_uid: str,
        *,
        on_complete: AccountRefreshComplete | None = None,
    ) -> None:
        account = self._accounts_by_uid.get(account_uid)
        folder_list = self._folder_lists.get(account_uid)
        if account is None or folder_list is None:
            if on_complete is not None:
                on_complete(0, ValueError(f"Account not loaded: {account_uid}"))
            return

        if on_complete is not None:
            self._account_reload_callbacks[account_uid] = on_complete

        self._sidebar_selecting = True
        try:
            _debug_listbox_start(
                "reload_account_clear",
                account_uid=account_uid,
                selecting=True,
                load_generation=self._load_generation,
            )
            try:
                self._clear_listbox(folder_list)
                folder_list.append(self._make_loading_row("Loading Folders…"))
            finally:
                _debug_listbox_end(
                    "reload_account_clear",
                    account_uid=account_uid,
                    load_generation=self._load_generation,
                )
        finally:
            self._sidebar_selecting = False
        # Count this load against pending so a mid-startup reload cannot make
        # folder_tree_ready flip early and leave search stuck disabled (#196).
        self._folder_loads_pending += 1
        if not self._folder_tree_ready and self._startup_folder_total > 0:
            self._startup_folder_total += 1
            self._update_startup_folder_load_status()
        self._start_folder_load(self._load_generation, account)

    def refresh_folder_row(
        self,
        account_uid: str,
        folder_name: str,
        *,
        on_complete: FolderRefreshComplete | None = None,
    ) -> None:
        if is_post_outbox_folder(folder_name):
            count = count_queued_for_account(account_uid)
            self.refresh_outbox_row(account_uid)
            if on_complete is not None:
                GLib.idle_add(
                    self._dispatch_folder_refresh_complete,
                    on_complete,
                    0,
                    count,
                    None,
                )
            return

        def worker() -> None:
            error: Exception | None = None
            unread = -1
            total = -1
            try:
                unread, total = self._mail.get_folder_stats(account_uid, folder_name)
            except Exception as exc:
                log_mail_error(
                    log,
                    f"Failed to refresh folder {account_uid}/{folder_name}",
                    exc,
                )
                error = exc
            GLib.idle_add(
                self._on_folder_row_refreshed,
                account_uid,
                folder_name,
                unread,
                total,
                error,
                on_complete,
            )

        # Qualname shows up in mail-I/O still-running warnings (#210).
        short = account_uid[:8] if account_uid else "?"
        worker.__qualname__ = (
            f"MailSidebar.refresh_folder_row[{short}/{folder_name}]"
        )
        get_mail_io_thread().submit(worker)

    @staticmethod
    def _dispatch_folder_refresh_complete(
        on_complete: FolderRefreshComplete,
        unread: int,
        total: int,
        error: Exception | None,
    ) -> bool:
        on_complete(unread, total, error)
        return False

    def _on_folder_row_refreshed(
        self,
        account_uid: str,
        folder_name: str,
        unread: int,
        total: int,
        error: Exception | None,
        on_complete: FolderRefreshComplete | None = None,
    ) -> bool:
        if error is None:
            self.update_folder_row(account_uid, folder_name, unread, total)
        if on_complete is not None:
            on_complete(unread, total, error)
        return False

    def _finish_account_reload(
        self, account_uid: str, folder_count: int, error: Exception | None
    ) -> None:
        callback = self._account_reload_callbacks.pop(account_uid, None)
        if callback is not None:
            callback(folder_count, error)

    def _setup_context_menu(self) -> None:
        specs = (
            ("new-folder", self._on_new_folder_activate),
            ("new-subfolder", self._on_new_subfolder_activate),
            ("rename-folder", self._on_rename_folder_activate),
            ("delete-folder", self._on_delete_folder_activate),
            ("archive-read", self._on_archive_read_activate),
            ("archive-read-unflagged", self._on_archive_read_unflagged_activate),
            ("archive-all", self._on_archive_all_activate),
            ("send-now", self._on_send_now_activate),
            ("empty-trash", self._on_empty_trash_activate),
            ("refresh", self._on_refresh_menu_activate),
            ("take-offline", self._on_take_offline_activate),
            ("take-online", self._on_take_online_activate),
        )
        group = Gio.SimpleActionGroup.new()
        for name, handler in specs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
            self._context_actions[name] = action
        self._widget.insert_action_group("sidebar", group)

    def _context_menu_state(
        self,
        account_uid: str,
        folder_name: str | None,
        *,
        unread: int,
        total: int,
        is_unified_inbox: bool = False,
    ) -> dict[str, bool]:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        folders = self._account_folders.get(account_uid, [])
        move_state = resolve_move_menu_state(
            folders,
            folder_name or "",
            archive_type=Camel.FolderInfoFlags.TYPE_ARCHIVE,
            trash_type=Camel.FolderInfoFlags.TYPE_TRASH,
            type_mask=Camel.FOLDER_TYPE_MASK,
        )
        account = self._accounts_by_uid.get(account_uid)
        backend = account.backend if account else None
        return resolve_sidebar_context_menu(
            folders=folders,
            folder_name=folder_name,
            inbox_name=self.inbox_folder_for_account(account_uid),
            trash_name=move_state.get("trash_folder"),
            archive_name=move_state.get("archive_folder"),
            unread=unread,
            total=total,
            outbox_count=count_queued_for_account(account_uid),
            folder_crud_enabled=account_supports_folder_crud(
                backend=backend
            ),
            network_available=self._network_available,
            account_user_online=get_account_user_online(account_uid),
            account_offline_toggle_enabled=account_supports_user_offline(backend),
            account_connect_health=self._mail.get_account_connect_health(account_uid),
            is_unified_inbox=is_unified_inbox,
        )

    def _build_context_menu_model(self, state: dict[str, bool]) -> Gio.Menu:
        menu = Gio.Menu()

        def append_item(label: str, action: str, key: str) -> None:
            if not state.get(f"show_{key}"):
                return
            self._context_actions[action.split(".")[-1]].set_enabled(
                bool(state.get(f"enable_{key}"))
            )
            menu.append(label, action)

        append_item("Refresh", "sidebar.refresh", "refresh")
        append_item("Take Offline", "sidebar.take-offline", "take_offline")
        append_item("Take Online", "sidebar.take-online", "take_online")
        append_item("Archive All", "sidebar.archive-all", "archive_all")
        append_item("Archive All Read", "sidebar.archive-read", "archive_read")
        append_item(
            "Archive All Read and Unflagged",
            "sidebar.archive-read-unflagged",
            "archive_read_unflagged",
        )
        append_item("Send Now", "sidebar.send-now", "send_now")
        append_item("Empty Trash", "sidebar.empty-trash", "empty_trash")
        append_item("New Folder…", "sidebar.new-folder", "new_folder")
        append_item("New Sub-Folder…", "sidebar.new-subfolder", "new_subfolder")
        append_item("Rename…", "sidebar.rename-folder", "rename")
        append_item("Delete", "sidebar.delete-folder", "delete")
        return menu

    def _hide_context_popover(self) -> None:
        popover = self._context_popover
        if popover is None:
            return
        self._context_popover = None
        if popover.get_visible():
            popover.popdown()
        parent = popover.get_parent()
        if parent is not None:
            popover.unparent()

    @staticmethod
    def _dialog_parent(widget: Gtk.Widget) -> Gtk.Window | None:
        root = widget.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _on_refresh_menu_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        if folder_name is None:
            if self._on_refresh_account is not None:
                self._on_refresh_account(account_uid)
            return
        if self._on_refresh_folder is not None:
            self._on_refresh_folder(account_uid, folder_name)

    def _on_take_offline_activate(self, *_args) -> None:
        # Dismiss before Camel work so the menu cannot stick if I/O is busy.
        self._hide_context_popover()
        self._set_account_user_online(False)

    def _on_take_online_activate(self, *_args) -> None:
        self._hide_context_popover()
        self._set_account_user_online(True)

    def _set_account_user_online(self, online: bool) -> None:
        if self._context_target is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target.get("folder_name")
        is_unified_inbox = bool(self._context_target.get("is_unified_inbox"))
        # Account headers (folder_name is None) or unified Inboxes rows.
        if folder_name is not None and not is_unified_inbox:
            return
        account = self._accounts_by_uid.get(account_uid)
        if account is None or not account_supports_user_offline(account.backend):
            return
        if online:
            health = self._mail.get_account_connect_health(account_uid)
            needs_goa_sign_in = (
                health == "needs_sign_in" and self._mail.account_uses_goa(account_uid)
            )
            if not get_account_user_online(account_uid):
                self._mail.set_account_user_online(account_uid, True)
                self._update_account_offline_marker(account_uid)
                if self._on_account_online_changed is not None:
                    self._on_account_online_changed(account_uid, True)
            if needs_goa_sign_in:
                # Expired M365/GOA tokens cannot be fixed by reconnect alone.
                if self._mail.open_online_accounts_settings():
                    self._set_status(
                        "Sign in again in Settings → Online Accounts, "
                        "then return here to reconnect"
                    )
                else:
                    self._set_status(
                        "Open Settings → Online Accounts to sign in again"
                    )
                if self._on_goa_reauth_requested is not None:
                    self._on_goa_reauth_requested(account_uid)
                return
            if health != "ok" and self._on_refresh_account is not None:
                # Password IMAP / other degraded: retry connect (may prompt).
                self._on_refresh_account(account_uid)
            return
        if get_account_user_online(account_uid):
            self._mail.set_account_user_online(account_uid, False)
            self._update_account_offline_marker(account_uid)
            if self._on_account_online_changed is not None:
                self._on_account_online_changed(account_uid, False)

    def _update_account_offline_marker(self, account_uid: str) -> None:
        account = self._accounts_by_uid.get(account_uid)
        remote = account_supports_user_offline(
            account.backend if account is not None else None
        )
        show, tooltip = account_not_online_badge(
            user_online=get_account_user_online(account_uid),
            connect_health=self._mail.get_account_connect_health(account_uid),
            network_available=self._network_available,
            remote_account=remote,
            transfer_state=self._mail.get_account_transfer_state(account_uid),
        )
        for icon in (
            self._account_offline_icons.get(account_uid),
            self._inbox_offline_icons.get(account_uid),
        ):
            if icon is None:
                continue
            icon.set_visible(show)
            if tooltip:
                icon.set_tooltip_text(tooltip)

    def refresh_account_online_marker(self, account_uid: str) -> None:
        self._update_account_offline_marker(account_uid)

    def refresh_all_account_online_markers(self) -> None:
        uids = set(self._account_offline_icons) | set(self._inbox_offline_icons)
        for account_uid in uids:
            self._update_account_offline_marker(account_uid)

    def _on_new_folder_activate(self, *_args) -> None:
        self._prompt_and_create_folder(parent_folder_name=None)

    def _on_new_subfolder_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        self._prompt_and_create_folder(
            parent_folder_name=self._context_target["folder_name"]
        )

    def _prompt_and_create_folder(self, *, parent_folder_name: str | None) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        if parent_folder_name is None:
            heading = "New Folder"
            body = "Enter a name for the new folder."
        else:
            heading = "New Sub-folder"
            body = "Enter a name for the new sub-folder."
        name = prompt_folder_name(
            parent,
            heading=heading,
            body=body,
            confirm_label="Create",
        )
        if not name:
            return
        self._run_folder_operation(
            lambda: self._mail.create_folder(account_uid, parent_folder_name, name),
            success_status=f"Created folder {name}",
            on_success=lambda _result: self._after_folder_tree_changed(account_uid),
            error_heading="Could not create folder",
        )

    def _on_rename_folder_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        display_name = self._context_target.get("display_name") or folder_name or ""
        if folder_name and "/" in folder_name:
            display_name = folder_name.rsplit("/", 1)[-1]
        name = prompt_folder_name(
            parent,
            heading="Rename Folder",
            body="Enter a new name for this folder.",
            initial=display_name,
            confirm_label="Rename",
        )
        if not name or not folder_name:
            return
        self._run_folder_operation(
            lambda: self._mail.rename_folder(account_uid, folder_name, name),
            success_status=f"Renamed folder to {name}",
            on_success=lambda new_name: self._after_folder_renamed(
                account_uid, folder_name, new_name
            ),
            error_heading="Could not rename folder",
        )

    def _on_delete_folder_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        display_name = self._context_target.get("display_name") or folder_name or "folder"
        if not folder_name:
            return
        if not confirm_action(
            parent,
            heading="Delete Folder?",
            body=f"Delete “{display_name}” and all messages it contains?",
            confirm_label="Delete",
            destructive=True,
        ):
            return
        self._run_folder_operation(
            lambda: self._mail.delete_folder(account_uid, folder_name) or folder_name,
            success_status=f"Deleted folder {display_name}",
            on_success=lambda _result: self._after_folder_deleted(account_uid, folder_name),
            error_heading="Could not delete folder",
        )

    def _on_archive_read_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        read_count = int(self._context_target.get("read_count") or 0)
        if not folder_name or read_count <= 0:
            return
        noun = "message" if read_count == 1 else "messages"
        if not confirm_action(
            parent,
            heading="Archive Read Messages?",
            body=f"Archive {read_count} read {noun} from this inbox?",
            confirm_label="Archive",
        ):
            return
        status_label = f"Archived {read_count} read {noun}"
        if self._on_move_started is not None:
            self._on_move_started(account_uid, folder_name)
        self._set_status(f"Archiving {read_count} read {noun}…")
        self._run_folder_operation(
            lambda: self._mail.archive_read_messages(account_uid, folder_name),
            success_status=status_label,
            on_success=lambda result: self._after_bulk_archive(
                account_uid, folder_name, result, status_label
            ),
            error_heading="Could not archive messages",
            folder_transfer=True,
        )

    def _on_archive_read_unflagged_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        if not folder_name:
            return
        try:
            read_unflagged_count = self._mail.count_read_unflagged_messages(
                account_uid, folder_name
            )
        except Exception as exc:
            show_error(
                parent,
                heading="Could not archive messages",
                body=str(exc),
            )
            return
        if read_unflagged_count <= 0:
            return
        noun = "message" if read_unflagged_count == 1 else "messages"
        if not confirm_action(
            parent,
            heading="Archive all Read and Unflagged Messages?",
            body=(
                f"Archive {read_unflagged_count} read and unflagged {noun} "
                "from this inbox?"
            ),
            confirm_label="Archive",
        ):
            return
        status_label = f"Archived {read_unflagged_count} read and unflagged {noun}"
        if self._on_move_started is not None:
            self._on_move_started(account_uid, folder_name)
        self._set_status(
            f"Archiving {read_unflagged_count} read and unflagged {noun}…"
        )
        self._run_folder_operation(
            lambda: self._mail.archive_read_unflagged_messages(
                account_uid, folder_name
            ),
            success_status=status_label,
            on_success=lambda result: self._after_bulk_archive(
                account_uid, folder_name, result, status_label
            ),
            error_heading="Could not archive messages",
            folder_transfer=True,
        )

    def _on_archive_all_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        total = int(self._context_target.get("total") or 0)
        if not folder_name or total <= 0:
            return
        noun = "message" if total == 1 else "messages"
        if not confirm_action(
            parent,
            heading="Archive all Messages?",
            body=f"Archive all {total} {noun} from this inbox?",
            confirm_label="Archive",
        ):
            return
        status_label = f"Archived {total} {noun}"
        if self._on_move_started is not None:
            self._on_move_started(account_uid, folder_name)
        self._set_status(f"Archiving {total} {noun}…")
        self._run_folder_operation(
            lambda: self._mail.archive_all_messages(account_uid, folder_name),
            success_status=status_label,
            on_success=lambda result: self._after_bulk_archive(
                account_uid, folder_name, result, status_label
            ),
            error_heading="Could not archive messages",
            folder_transfer=True,
        )

    def _on_send_now_activate(self, *_args) -> None:
        if self._on_send_outbox is not None:
            self._on_send_outbox()

    def _on_empty_trash_activate(self, *_args) -> None:
        if self._context_target is None:
            return
        parent = self._dialog_parent(self._widget)
        if parent is None:
            return
        account_uid = self._context_target["account_uid"]
        folder_name = self._context_target["folder_name"]
        total = int(self._context_target.get("total") or 0)
        if not folder_name or total <= 0:
            return
        noun = "message" if total == 1 else "messages"
        if not confirm_action(
            parent,
            heading="Empty Trash?",
            body=f"Permanently delete {total} {noun} from Trash?",
            confirm_label="Empty Trash",
            destructive=True,
        ):
            return
        self._run_folder_operation(
            lambda: self._mail.empty_folder(account_uid, folder_name),
            success_status=f"Emptied Trash ({total} {noun} deleted)",
            on_success=lambda _result: self._after_folder_contents_changed(
                account_uid, folder_name
            ),
            error_heading="Could not empty Trash",
        )

    def _run_folder_operation(
        self,
        operation: Callable[[], object],
        *,
        success_status: str,
        on_success: Callable[[object], None],
        error_heading: str,
        folder_transfer: bool = False,
    ) -> None:
        def worker() -> None:
            error: Exception | None = None
            result: object | None = None
            try:
                result = operation()
            except Exception as exc:
                log_mail_error(log, error_heading, exc)
                error = exc
            finally:
                if folder_transfer:
                    self._mail.end_folder_transfer()
            GLib.idle_add(
                self._on_folder_operation_finished,
                result,
                error,
                success_status,
                on_success,
                error_heading,
            )

        if folder_transfer:
            self._mail.begin_folder_transfer()
        get_mail_io_thread().submit(worker)

    def _on_folder_operation_finished(
        self,
        result: object | None,
        error: Exception | None,
        success_status: str,
        on_success: Callable[[object], None],
        error_heading: str,
    ) -> bool:
        if error is not None:
            parent = self._dialog_parent(self._widget)
            if parent is not None:
                show_error(parent, error_heading, str(error))
            else:
                self._set_status(f"{error_heading}: {error}")
            return False
        self._set_status(success_status)
        if result is not None:
            on_success(result)
        else:
            on_success(None)
        return False

    def _after_folder_tree_changed(
        self, account_uid: str, *, removed_folder: str | None = None
    ) -> None:
        self.reload_account(account_uid)
        if self._on_folder_tree_changed is not None:
            self._on_folder_tree_changed(account_uid, removed_folder)

    def _after_folder_renamed(
        self, account_uid: str, old_name: str, new_name: object
    ) -> None:
        if not isinstance(new_name, str):
            self._after_folder_tree_changed(account_uid)
            return
        if self._saved_active_folder == (account_uid, old_name):
            self._saved_active_folder = (account_uid, new_name)
            self._activated_folder = (account_uid, new_name)
        self._after_folder_tree_changed(account_uid)

    def _after_folder_deleted(self, account_uid: str, folder_name: str) -> None:
        if self._saved_active_folder == (account_uid, folder_name):
            self._saved_active_folder = None
            self._activated_folder = None
        self._after_folder_tree_changed(account_uid, removed_folder=folder_name)

    def _after_folder_contents_changed(
        self, account_uid: str, folder_name: str
    ) -> None:
        self.refresh_folder_row(account_uid, folder_name)
        if self._on_folder_contents_changed is not None:
            self._on_folder_contents_changed(account_uid, folder_name)

    def _after_bulk_archive(
        self,
        account_uid: str,
        folder_name: str,
        result: object,
        status_label: str,
    ) -> None:
        self._after_folder_contents_changed(account_uid, folder_name)
        if not isinstance(result, dict):
            return
        archived_count = int(result.get("archived_count") or 0)
        if archived_count <= 0:
            # Overwrite the premature success flash from _on_folder_operation_finished.
            self._set_status("No messages were archived")
            return
        self._set_status(status_label)
        if self._on_move_undo_available is not None:
            self._on_move_undo_available(
                account_uid, folder_name, result, status_label
            )

    def _attach_refresh_menu(
        self,
        widget: Gtk.Widget,
        *,
        account_uid: str,
        folder_name: str | None,
        is_unified_inbox: bool = False,
    ) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(0)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect(
            "pressed",
            self._on_sidebar_context_pressed,
            account_uid,
            folder_name,
            is_unified_inbox,
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
        is_unified_inbox: bool = False,
        *,
        unread: int = -1,
        total: int = -1,
        display_name: str | None = None,
    ) -> None:
        if n_press != 1:
            return
        event = gesture.get_current_event()
        if event is None or not Gdk.Event.triggers_context_menu(event):
            return

        widget = gesture.get_widget()
        if widget is None:
            return

        row = widget.get_ancestor(Gtk.ListBoxRow)
        if isinstance(row, Gtk.ListBoxRow):
            unread = int(getattr(row, "unread", unread))
            total = int(getattr(row, "total", total))
            display_name = getattr(row, "display_name", display_name)
            if getattr(row, "is_unified_inbox", False):
                is_unified_inbox = True

        if is_post_outbox_folder(folder_name):
            total = count_queued_for_account(account_uid)
            unread = 0

        state = self._context_menu_state(
            account_uid,
            folder_name,
            unread=unread,
            total=total,
            is_unified_inbox=is_unified_inbox,
        )
        self._context_target = {
            "account_uid": account_uid,
            "folder_name": folder_name,
            "display_name": display_name,
            "unread": unread,
            "total": total,
            "read_count": state.get("read_count", 0),
            "is_unified_inbox": is_unified_inbox,
        }

        menu = self._build_context_menu_model(state)
        if menu.get_n_items() == 0:
            return
        self._hide_context_popover()
        self._context_popover = Gtk.PopoverMenu.new_from_model(menu)
        self._context_popover.set_has_arrow(False)
        self._context_popover.set_parent(self._widget)
        origin = widget.translate_coordinates(self._widget, x, y)
        pop_x, pop_y = origin if origin is not None else (int(x), int(y))
        rect = Gdk.Rectangle()
        rect.x = int(pop_x)
        rect.y = int(pop_y)
        rect.width = 1
        rect.height = 1
        self._context_popover.set_pointing_to(rect)
        self._context_popover.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _start_folder_load(self, load_id: int, account: MailAccount) -> None:
        def worker() -> None:
            cancellable = Gio.Cancellable()
            self._mail._register_folder_list_cancellable(cancellable)
            error: Exception | None = None
            folders: list[dict] | None = None
            cancelled = False
            try:
                if cancellable.is_cancelled():
                    cancelled = True
                else:
                    folders = self._mail.list_folders(
                        account.uid, cancellable=cancellable
                    )
            except GLib.Error as exc:
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    cancelled = True
                else:
                    log_mail_error(
                        log, f"Failed to list folders for {account.uid}", exc
                    )
                    error = exc
            except Exception as exc:
                log_mail_error(log, f"Failed to list folders for {account.uid}", exc)
                error = exc
            finally:
                self._mail._unregister_folder_list_cancellable(cancellable)
            if cancelled:
                GLib.idle_add(
                    self._on_folder_load_cancelled, load_id, account.uid
                )
                return
            GLib.idle_add(
                self._on_folders_loaded,
                load_id,
                account.uid,
                folders,
                error,
            )

        get_mail_io_thread().submit_background(worker)

    def _on_folder_load_cancelled(self, load_id: int, account_uid: str) -> bool:
        # Stale generations must not touch the current pending counter — that
        # races with sidebar.reload()/load() and can mark the tree ready before
        # rows exist, leaving search disabled (#196).
        if load_id != self._load_generation:
            return False
        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            self._release_folder_load_slot()
            return False
        GLib.timeout_add(250, self._retry_folder_load, load_id, account_uid)
        return False

    def _retry_folder_load(self, load_id: int, account_uid: str) -> bool:
        if load_id != self._load_generation:
            return False
        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            self._release_folder_load_slot()
            return False
        self._start_folder_load(load_id, account)
        return False

    def _release_folder_load_slot(self) -> None:
        self._folder_loads_pending = max(0, self._folder_loads_pending - 1)
        self._update_startup_folder_load_status()
        self._maybe_finish_initial_folder_load()

    def _update_startup_folder_load_status(self) -> None:
        if self._folder_tree_ready or self._startup_folder_total <= 0:
            return
        done = self._startup_folder_total - self._folder_loads_pending
        self._set_status(
            format_startup_loading_folders(done, self._startup_folder_total)
        )

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
            self._release_folder_load_slot()
            return False

        # Block row-selected → activate while rebuilding. Gtk.ListBox SINGLE
        # selection fires row-selected on each append; re-entering activate /
        # unselect_all from inside append corrupts the list sequence (segfault).
        already_selecting = self._sidebar_selecting
        self._sidebar_selecting = True
        load_error = error
        folder_count = 0
        _debug_listbox_start(
            "folders_loaded_rebuild",
            load_id=load_id,
            account_uid=account_uid,
            already_selecting=already_selecting,
            load_generation=self._load_generation,
            has_error=load_error is not None,
            folder_count=0 if folders is None else len(folders),
        )
        try:
            _debug_listbox_start(
                "folders_loaded_clear",
                load_id=load_id,
                account_uid=account_uid,
            )
            try:
                self._clear_listbox(folder_list)
            finally:
                _debug_listbox_end(
                    "folders_loaded_clear",
                    load_id=load_id,
                    account_uid=account_uid,
                )

            if load_error is not None:
                label_text = format_folder_load_error(load_error)
                error_label = Gtk.Label(
                    label=label_text,
                    xalign=0,
                    wrap=True,
                )
                error_label.add_css_class("dim-label")
                error_label.set_margin_start(12)
                error_label.set_margin_end(12)
                error_label.set_margin_bottom(8)
                _debug_listbox_start(
                    "folders_loaded_error_row",
                    load_id=load_id,
                    account_uid=account_uid,
                )
                try:
                    folder_list.append(self._wrap_list_row(error_label))
                    self._add_outbox_row(account_uid)
                    health = self._mail.get_account_connect_health(account_uid)
                    if health == "ok":
                        if is_network_unavailable_error(load_error):
                            new_health = "not_connected"
                        elif is_sign_in_required_error(load_error):
                            new_health = "needs_sign_in"
                        else:
                            new_health = "not_connected"
                        self._mail.set_account_connect_health(account_uid, new_health)
                    # Keep offline/degraded accounts visible in the unified Inboxes list.
                    self._add_inbox_row_unavailable(account_uid)
                    self._update_account_offline_marker(account_uid)
                finally:
                    _debug_listbox_end(
                        "folders_loaded_error_row",
                        load_id=load_id,
                        account_uid=account_uid,
                    )
            else:
                assert folders is not None
                folders = filter_sidebar_folders(folders)
                self._account_folders[account_uid] = folders
                folder_count = len(folders)
                _debug_listbox_start(
                    "folders_loaded_append_rows",
                    load_id=load_id,
                    account_uid=account_uid,
                    folder_count=folder_count,
                )
                try:
                    for index, folder in enumerate(folders):
                        _debug_listbox_start(
                            "folders_loaded_append_one",
                            load_id=load_id,
                            account_uid=account_uid,
                            index=index,
                            folder=folder.get("full_name"),
                        )
                        try:
                            folder_list.append(
                                self._make_folder_row(account_uid, folder)
                            )
                        finally:
                            _debug_listbox_end(
                                "folders_loaded_append_one",
                                load_id=load_id,
                                account_uid=account_uid,
                                index=index,
                                folder=folder.get("full_name"),
                            )

                    self._add_outbox_row(account_uid)
                    self._add_inbox_row(account_uid, folders)
                finally:
                    _debug_listbox_end(
                        "folders_loaded_append_rows",
                        load_id=load_id,
                        account_uid=account_uid,
                        folder_count=folder_count,
                    )
        finally:
            self._sidebar_selecting = False
            _debug_listbox_end(
                "folders_loaded_rebuild",
                load_id=load_id,
                account_uid=account_uid,
                folder_count=folder_count,
                has_error=load_error is not None,
            )

        if load_error is not None:
            self._folder_loads_pending = max(0, self._folder_loads_pending - 1)
            self._finish_account_reload(account_uid, 0, load_error)
            self._update_startup_folder_load_status()
            # Select before marking ready so search can enable in the same turn (#196).
            self._maybe_apply_initial_selection()
            self._maybe_finish_initial_folder_load()
            self._maybe_recover_search_folder_selection()
            return False

        # Eager inbox only — full-folder STATUS at tree load saturates Camel/mail
        # I/O and can make Post unresponsive (or OOM) on large accounts (#170).
        self.refresh_inbox_counts(account_uid)
        self._update_account_offline_marker(account_uid)

        self._folder_loads_pending = max(0, self._folder_loads_pending - 1)
        self._maybe_apply_initial_selection()
        self._finish_account_reload(account_uid, folder_count, None)
        self._update_startup_folder_load_status()
        self._maybe_finish_initial_folder_load()
        self._maybe_recover_search_folder_selection()

        return False

    def _maybe_finish_initial_folder_load(self) -> None:
        if self._folder_loads_pending > 0:
            return
        self._folder_tree_ready = True
        if self._startup_folder_total > 0:
            self._set_status(f"{len(self._accounts)} account(s)")
            self._startup_folder_total = 0
        # Enable search before offline-sync kickoff: list_accounts() there can
        # throw and would otherwise leave the search bar stuck disabled (#196).
        if self._on_folder_tree_ready is not None:
            self._on_folder_tree_ready()
        callback = self._on_initial_folder_load_complete
        if callback is not None:
            self._on_initial_folder_load_complete = None
            try:
                callback()
            except Exception:
                log.exception("Initial folder-load complete callback failed")

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
        self._hide_context_popover()
        self._folder_count_poll_generation += 1
        while child := self._sidebar_box.get_first_child():
            self._sidebar_box.remove(child)
        self._folder_lists.clear()
        self._account_folders.clear()
        self._account_inbox_folders.clear()
        self._account_offline_icons.clear()
        self._inbox_offline_icons.clear()
        self._inbox_expander = None
        self._inbox_list = None

    def _all_folder_listboxes(self) -> list[Gtk.ListBox]:
        lists = list(self._folder_lists.values())
        if self._inbox_list is not None:
            lists.append(self._inbox_list)
        return lists

    def _maybe_apply_initial_selection(self) -> None:
        if not self._needs_initial_selection:
            return
        initial_list, initial_row = self._find_initial_folder()
        if initial_list is None or initial_row is None:
            if self._folder_loads_pending > 0:
                return
            self._needs_initial_selection = False
            return
        self._activate_folder_row(initial_list, initial_row)
        self._needs_initial_selection = False

    def ensure_folder_selection(self) -> None:
        """Select a searchable folder if none is active yet (#196).

        Called when the folder tree becomes ready so the header search bar can
        enable without waiting for a manual sidebar click.
        """
        if self._activated_folder is not None:
            account_uid, folder_name = self._activated_folder
            if not is_post_outbox_folder(folder_name):
                account = self._accounts_by_uid.get(account_uid)
                if account is not None:
                    self._on_folder_selected(account, folder_name)
                    return
        self._needs_initial_selection = True
        self._maybe_apply_initial_selection()

    def _maybe_recover_search_folder_selection(self) -> None:
        """If the tree is already ready but no folder is active, select one (#196).

        Covers the case where folder_tree_ready flipped before rows existed
        (stale load completions / reload races); later row builds must still
        drive a selection so search can enable.
        """
        if not self._folder_tree_ready:
            return
        if self._activated_folder is not None:
            account_uid, folder_name = self._activated_folder
            if not is_post_outbox_folder(folder_name):
                return
        self.ensure_folder_selection()

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
            if self._folder_loads_pending > 0:
                return None, None

        if self._folder_loads_pending > 0:
            return None, None

        return self._default_initial_folder()

    def _default_initial_folder(
        self,
    ) -> tuple[Gtk.ListBox | None, Gtk.ListBoxRow | None]:
        if self._inbox_list is not None:
            row = self._inbox_list.get_first_child()
            while row is not None:
                folder_name = getattr(row, "folder_name", None)
                if folder_name and not is_post_outbox_folder(folder_name):
                    return self._inbox_list, row
                row = row.get_next_sibling()

        first: tuple[Gtk.ListBox | None, Gtk.ListBoxRow | None] = (None, None)
        for listbox in self._folder_lists.values():
            row = listbox.get_first_child()
            while row is not None:
                folder_name = getattr(row, "folder_name", None)
                if folder_name and not is_post_outbox_folder(folder_name):
                    if first[1] is None:
                        first = (listbox, row)
                    if folder_name.upper() in ("INBOX", "INBOX/"):
                        return listbox, row
                row = row.get_next_sibling()
        return first

    def _make_inbox_section_loading(self) -> Gtk.Expander:
        expander = Gtk.Expander()
        expander.add_css_class("sidebar-section")
        expander.set_expanded(self._inbox_expanded)
        expander.connect("notify::expanded", self._on_inbox_expanded)
        header = Gtk.Label(label="Inboxes", xalign=0)
        header.add_css_class("heading")
        header.set_margin_bottom(4)
        expander.set_label_widget(header)
        expander.set_margin_start(6)
        expander.set_margin_end(6)
        expander.set_margin_top(0)

        inbox_list = Gtk.ListBox()
        inbox_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        inbox_list.add_css_class("navigation-sidebar")
        inbox_list.connect("row-selected", self._on_folder_row_selected)
        self._setup_inbox_list_dnd(inbox_list)
        self._inbox_list = inbox_list
        self._inbox_expander = expander

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

        self._replace_inbox_row(account_uid, inbox_folder)

    def _add_inbox_row_unavailable(self, account_uid: str) -> None:
        """Show a degraded/offline account in Inboxes even when folder list failed."""
        if self._inbox_list is None:
            return
        cached_name = self._account_inbox_folders.get(account_uid)
        if not cached_name:
            cached_name = self._mail.get_inbox_folder_name_cached(account_uid)
        inbox_name = cached_name or "INBOX"
        inbox_folder = {
            "full_name": inbox_name,
            "display_name": "Inbox",
            "unread": -1,
            "total": -1,
            "flags": 0,
        }
        self._replace_inbox_row(account_uid, inbox_folder)

    def _replace_inbox_row(self, account_uid: str, inbox_folder: dict) -> None:
        if self._inbox_list is None:
            return

        _debug_listbox_start(
            "replace_inbox_row",
            account_uid=account_uid,
            selecting=self._sidebar_selecting,
            folder=inbox_folder.get("full_name"),
        )
        try:
            row = self._inbox_list.get_first_child()
            while row is not None:
                next_row = row.get_next_sibling()
                if getattr(row, "account_uid", None) == account_uid:
                    self._inbox_list.remove(row)
                row = next_row

            account = self._accounts_by_uid.get(account_uid)
            display = account.display_label if account else account_uid
            full_name = inbox_folder.get("full_name")
            if isinstance(full_name, str) and full_name:
                self._account_inbox_folders[account_uid] = full_name
            self._inbox_list.append(
                self._make_folder_row(
                    account_uid, inbox_folder, display=display, show_offline_badge=True
                )
            )
            row = self._inbox_list.get_last_child()
            if isinstance(row, Gtk.ListBoxRow):
                self._setup_inbox_row_drag(row)
            self._sort_inbox_list()
            self._update_account_offline_marker(account_uid)
        finally:
            _debug_listbox_end(
                "replace_inbox_row",
                account_uid=account_uid,
                selecting=self._sidebar_selecting,
            )

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

        _debug_listbox_start(
            "sort_inbox_list",
            selecting=self._sidebar_selecting,
        )
        try:
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
        finally:
            _debug_listbox_end(
                "sort_inbox_list",
                selecting=self._sidebar_selecting,
            )

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
                was_selecting = self._sidebar_selecting
                self._sidebar_selecting = True
                try:
                    order = self._current_inbox_order_from_list()
                    if source_uid not in order:
                        return False
                    order.remove(source_uid)
                    order.append(source_uid)
                    self._inbox_order = order
                    self._sort_inbox_list()
                    self._persist_view_state()
                    return True
                finally:
                    self._sidebar_selecting = was_selecting

            target_uid = getattr(target_row, "account_uid", None)
            if not target_uid or target_uid == source_uid:
                return False

            allocation = target_row.get_allocation()
            after = y > allocation.y + allocation.height / 2
            was_selecting = self._sidebar_selecting
            self._sidebar_selecting = True
            try:
                self._move_inbox_row(source_uid, target_uid, after=after)
                return True
            finally:
                self._sidebar_selecting = was_selecting

        drop_target.connect("drop", drop)
        inbox_list.add_controller(drop_target)

    def _make_account_section_loading(self, account: MailAccount) -> Gtk.Expander:
        expander = Gtk.Expander()
        expander.add_css_class("sidebar-section")
        expander.set_expanded(self._expanded_accounts.get(account.uid, True))
        expander.connect("notify::expanded", self._on_account_expanded, account.uid)
        expander.set_label_widget(self._make_account_header(account))
        expander.set_margin_start(6)
        expander.set_margin_end(6)
        expander.set_margin_top(0 if self._sidebar_box.get_first_child() is None else 4)

        folder_list = Gtk.ListBox()
        folder_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        folder_list.add_css_class("navigation-sidebar")
        folder_list.connect("row-selected", self._on_folder_row_selected)
        self._folder_lists[account.uid] = folder_list

        folder_list.append(self._make_loading_row("Loading Folders…"))

        expander.set_child(folder_list)
        return expander

    def _on_account_expanded(self, expander: Gtk.Expander, _pspec, account_uid: str) -> None:
        self._expanded_accounts[account_uid] = expander.get_expanded()
        self._persist_view_state()

    def _make_account_header(self, account: MailAccount) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=account.display_label, xalign=0, hexpand=True)
        label.add_css_class("heading")
        label.set_ellipsize(3)
        label.set_margin_bottom(4)
        offline_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        offline_icon.set_tooltip_text("Account Offline")
        offline_icon.add_css_class("dim-label")
        self._account_offline_icons[account.uid] = offline_icon
        self._update_account_offline_marker(account.uid)
        box.append(label)
        box.append(offline_icon)
        self._attach_refresh_menu(box, account_uid=account.uid, folder_name=None)
        return box

    @staticmethod
    def _make_loading_row(text: str) -> Gtk.ListBoxRow:
        label = Gtk.Label(label=text, xalign=0, margin_start=12, margin_end=12)
        label.add_css_class("dim-label")
        label.set_valign(Gtk.Align.CENTER)
        row = Gtk.ListBoxRow()
        row.set_child(label)
        row.set_activatable(False)
        row.set_selectable(False)
        return row

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
        show_offline_badge: bool = False,
    ) -> Gtk.ListBoxRow:
        if display is None:
            display = folder.get("display_name") or folder.get("full_name") or "?"
        unread = folder.get("unread", -1)
        total = folder.get("total", -1)
        folder_name = folder.get("full_name")
        pending = False
        if (
            isinstance(folder_name, str)
            and is_heavy_folder_name(folder_name)
            and total < 0
            and self._network_available
            and not account_is_user_offline(account_uid)
            and folder_status_cache.load(account_uid, folder_name) is None
        ):
            self._heavy_status_pending.add((account_uid, folder_name))
            pending = True
        label_text = format_folder_label(
            display, unread, total, status_pending=pending
        )

        label = Gtk.Label(label=label_text, xalign=0, hexpand=True)
        label.set_margin_start(12)
        label.set_margin_end(6 if show_offline_badge else 12)
        row = Gtk.ListBoxRow()
        if show_offline_badge:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            offline_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
            offline_icon.add_css_class("dim-label")
            offline_icon.set_margin_end(12)
            offline_icon.set_visible(False)
            self._inbox_offline_icons[account_uid] = offline_icon
            box.append(label)
            box.append(offline_icon)
            row.set_child(box)
            self._update_account_offline_marker(account_uid)
        else:
            row.set_child(label)
        row.account_uid = account_uid
        row.folder_name = folder_name
        row.display_name = display
        row.unread = unread
        row.total = total
        row.is_unified_inbox = show_offline_badge
        if row.folder_name:
            self._attach_refresh_menu(
                row,
                account_uid=account_uid,
                folder_name=row.folder_name,
                is_unified_inbox=show_offline_badge,
            )
            self._setup_folder_row_drop(row)
        return row

    def _setup_folder_row_drop(self, row: Gtk.ListBoxRow) -> None:
        if self._on_messages_dropped is None:
            return

        formats = Gdk.ContentFormats.new([MESSAGE_TRANSFER_MIME])
        drop_target = Gtk.DropTarget.new(formats, Gdk.DragAction.MOVE)

        def enter(
            _target: Gtk.DropTarget, _x: float, _y: float
        ) -> Gdk.DragAction:
            row.add_css_class("drop-highlight")
            return Gdk.DragAction.MOVE

        def leave(_target: Gtk.DropTarget) -> None:
            row.remove_css_class("drop-highlight")

        def drop(
            _target: Gtk.DropTarget,
            value: object,
            _x: float,
            _y: float,
        ) -> bool:
            row.remove_css_class("drop-highlight")
            if not isinstance(value, GLib.Bytes):
                return False
            payload = decode_message_transfer(bytes(value.get_data()))
            if payload is None:
                return False
            account_uid = getattr(row, "account_uid", None)
            folder_name = getattr(row, "folder_name", None)
            if not account_uid or not folder_name:
                return False
            if not validate_message_drop(
                payload,
                dest_account_uid=account_uid,
                dest_folder=folder_name,
                dest_is_outbox=is_post_outbox_folder(folder_name),
            ):
                return False
            self._on_messages_dropped(
                payload.account_uid,
                payload.source_folder,
                folder_name,
                list(payload.uids),
            )
            return True

        drop_target.connect("enter", enter)
        drop_target.connect("leave", leave)
        drop_target.connect("drop", drop)
        row.add_controller(drop_target)

    def _add_outbox_row(self, account_uid: str) -> None:
        folder_list = self._folder_lists.get(account_uid)
        if folder_list is None:
            return
        _debug_listbox_start(
            "add_outbox_row",
            account_uid=account_uid,
            selecting=self._sidebar_selecting,
        )
        try:
            count = count_queued_for_account(account_uid)
            folder_list.append(
                self._make_folder_row(
                    account_uid,
                    outbox_folder_dict(count),
                    display="Outbox",
                )
            )
        finally:
            _debug_listbox_end(
                "add_outbox_row",
                account_uid=account_uid,
            )

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        _debug_listbox_start("clear_listbox")
        try:
            while child := listbox.get_first_child():
                listbox.remove(child)
        finally:
            _debug_listbox_end("clear_listbox")

    def _on_folder_row_selected(
        self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None or self._sidebar_selecting:
            return
        self._activate_folder_row(listbox, row)

    def _find_folder_row(
        self,
        listbox: Gtk.ListBox,
        account_uid: str,
        folder_name: str,
    ) -> Gtk.ListBoxRow | None:
        row = listbox.get_first_child()
        while row is not None:
            if (
                getattr(row, "account_uid", None) == account_uid
                and getattr(row, "folder_name", None) == folder_name
            ):
                return row
            row = row.get_next_sibling()
        return None

    def mark_folder_active(self, account_uid: str, folder_name: str) -> None:
        """Record active folder without firing on_folder_selected (eager launch restore)."""
        self._activated_folder = (account_uid, folder_name)

    def clear_folder_selection(self) -> None:
        """Clear sidebar highlights without dropping the saved folder for restore."""
        _debug_listbox_start(
            "clear_folder_selection",
            selecting=self._sidebar_selecting,
        )
        self._sidebar_selecting = True
        try:
            for listbox in self._all_folder_listboxes():
                listbox.unselect_all()
        finally:
            self._sidebar_selecting = False
            _debug_listbox_end("clear_folder_selection")
        self._activated_folder = None

    def restore_folder_selection(self, account_uid: str, folder_name: str) -> bool:
        """Re-select a folder row and notify via on_folder_selected."""
        for listbox in self._all_folder_listboxes():
            row = self._find_folder_row(listbox, account_uid, folder_name)
            if row is not None:
                self._activate_folder_row(listbox, row)
                return True
        return False

    def _sync_folder_row_selection(
        self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow
    ) -> None:
        account_uid = getattr(row, "account_uid", None)
        folder_name = getattr(row, "folder_name", None)
        if not account_uid or not folder_name:
            return

        _debug_listbox_start(
            "sync_folder_row_selection",
            account_uid=account_uid,
            folder_name=folder_name,
            selecting=self._sidebar_selecting,
        )
        already_selecting = self._sidebar_selecting
        # (#201) Gtk may emit selection callbacks during unselect_all/select_row
        # transitions; keep the guard enabled for the whole phase.
        self._sidebar_selecting = True
        try:
            for other in self._all_folder_listboxes():
                if other is not listbox:
                    other.unselect_all()

            listbox.select_row(row)
            for other in self._all_folder_listboxes():
                if other is listbox:
                    continue
                mirror = self._find_folder_row(other, account_uid, folder_name)
                if mirror is not None:
                    other.select_row(mirror)
        finally:
            self._sidebar_selecting = already_selecting
            _debug_listbox_end(
                "sync_folder_row_selection",
                account_uid=account_uid,
                folder_name=folder_name,
            )

    def _activate_folder_row(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        account_uid = getattr(row, "account_uid", None)
        folder_name = getattr(row, "folder_name", None)
        if not account_uid or not folder_name:
            return

        selection = (account_uid, folder_name)
        account = self._accounts_by_uid.get(account_uid)
        if selection == self._activated_folder:
            self._sync_folder_row_selection(listbox, row)
            # Still notify so header search can enable after folder-tree ready
            # even when the row was only marked active (eager restore) (#196).
            if account is not None:
                self._on_folder_selected(account, folder_name)
            return

        self._sync_folder_row_selection(listbox, row)

        if account is None:
            return

        self._activated_folder = selection
        self._saved_active_folder = selection
        self._persist_view_state()
        self._on_folder_selected(account, folder_name)
