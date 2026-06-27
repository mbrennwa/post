# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watch Camel stores/folders for server-side mail changes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Camel", "1.2")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")

from gi.repository import Camel, GLib, GObject

from .eds import MailService
from .folders import is_post_local_folder
from .io_thread import get_mail_io_thread
from .send_queue import is_network_unavailable_error, log_mail_error

log = logging.getLogger(__name__)

OnFolderChanged = Callable[[str, str], None]
OnFolderTreeChanged = Callable[[str], None]

_DEBOUNCE_MS = 400


@dataclass
class _AccountWatch:
    account_uid: str
    store: Camel.Store
    inbox_folder_name: str | None = None
    store_handler_ids: list[int] = field(default_factory=list)
    folder_handler_ids: dict[str, int] = field(default_factory=dict)
    watched_folders: dict[str, Camel.Folder] = field(default_factory=dict)


class MailSyncWatcher:
    """Subscribe to Camel change signals and notify the UI."""

    def __init__(
        self,
        mail: MailService,
        *,
        on_folder_changed: OnFolderChanged,
        on_folder_tree_changed: OnFolderTreeChanged,
    ) -> None:
        self._mail = mail
        self._on_folder_changed = on_folder_changed
        self._on_folder_tree_changed = on_folder_tree_changed
        self._running = False
        self._account_uids: list[str] = []
        self._current_account_uid: str | None = None
        self._current_folder_name: str | None = None
        self._accounts: dict[str, _AccountWatch] = {}
        self._store_to_account: dict[int, str] = {}
        self._folder_to_account: dict[int, tuple[str, str]] = {}
        self._debounce_ids: dict[tuple[str, str], int] = {}
        self._setup_generation = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule_setup()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._setup_generation += 1
        self._clear_debounce_timers()
        for watch in list(self._accounts.values()):
            self._disconnect_account(watch)
        self._accounts.clear()
        self._store_to_account.clear()
        self._folder_to_account.clear()

    def set_accounts(self, account_uids: list[str]) -> None:
        self._account_uids = list(account_uids)
        if self._running:
            self._schedule_setup()

    def set_current_folder(
        self, account_uid: str | None, folder_name: str | None
    ) -> None:
        self._current_account_uid = account_uid
        self._current_folder_name = folder_name
        if self._running:
            self._schedule_setup()

    def _schedule_setup(self) -> None:
        self._setup_generation += 1
        generation = self._setup_generation
        account_uids = list(self._account_uids)
        current_account_uid = self._current_account_uid
        current_folder_name = self._current_folder_name

        def worker() -> None:
            setups: list[tuple[str, Camel.Store, str | None, str | None]] = []
            for account_uid in account_uids:
                inbox_name: str | None = None
                current_name: str | None = None
                try:
                    store = self._mail.get_store_for_sync(account_uid)
                    inbox_name = self._mail.get_inbox_folder_name(account_uid)
                    if account_uid == current_account_uid and current_folder_name:
                        current_name = current_folder_name
                except Exception as exc:
                    log_mail_error(
                        log,
                        f"Could not prepare sync watch for account {account_uid}",
                        exc,
                    )
                    continue
                setups.append((account_uid, store, inbox_name, current_name))

            GLib.idle_add(
                self._apply_setup,
                generation,
                setups,
            )

        get_mail_io_thread().submit(worker)

    def _apply_setup(
        self,
        generation: int,
        setups: list[tuple[str, Camel.Store, str | None, str | None]],
    ) -> bool:
        if not self._running or generation != self._setup_generation:
            return False

        desired_uids = {account_uid for account_uid, *_rest in setups}
        for account_uid in list(self._accounts):
            if account_uid not in desired_uids:
                watch = self._accounts.pop(account_uid)
                self._store_to_account.pop(id(watch.store), None)
                self._disconnect_account(watch)

        for account_uid, store, inbox_name, current_name in setups:
            watch = self._accounts.get(account_uid)
            if watch is None or watch.store is not store:
                if watch is not None:
                    self._store_to_account.pop(id(watch.store), None)
                    self._disconnect_account(watch)
                watch = _AccountWatch(account_uid=account_uid, store=store)
                self._accounts[account_uid] = watch
                self._store_to_account[id(store)] = account_uid
                self._connect_store_signals(watch)
            watch.inbox_folder_name = inbox_name
            self._update_watched_folders(watch, inbox_name, current_name)

        return False

    def _connect_store_signals(self, watch: _AccountWatch) -> None:
        store = watch.store
        for signal_name in (
            "folder-info-stale",
            "folder-created",
            "folder-deleted",
            "folder-renamed",
        ):
            # Camel.Service.connect() is for network I/O, not GObject signals.
            handler_id = GObject.Object.connect(
                store, signal_name, self._on_store_tree_event
            )
            watch.store_handler_ids.append(handler_id)

    def _on_store_tree_event(self, store: Camel.Store, *_args: object) -> None:
        if not self._running:
            return
        account_uid = self._store_to_account.get(id(store))
        if account_uid is None:
            return
        self._on_folder_tree_changed(account_uid)

    def _update_watched_folders(
        self,
        watch: _AccountWatch,
        inbox_name: str | None,
        current_name: str | None,
    ) -> None:
        desired_names: set[str] = set()
        if inbox_name:
            desired_names.add(inbox_name)
        if current_name and not is_post_local_folder(current_name):
            desired_names.add(current_name)

        for folder_name in list(watch.watched_folders):
            if folder_name not in desired_names:
                self._disconnect_folder(watch, folder_name)

        for folder_name in desired_names:
            if folder_name in watch.watched_folders:
                continue
            try:
                folder = watch.store.get_folder_sync(folder_name, 0, None)
            except Exception as exc:
                log_mail_error(
                    log,
                    f"Could not open folder {watch.account_uid}/{folder_name} for sync watch",
                    exc,
                )
                continue
            if folder is None:
                continue
            handler_id = folder.connect("changed", self._on_folder_changed_signal)
            watch.folder_handler_ids[folder_name] = handler_id
            watch.watched_folders[folder_name] = folder
            self._folder_to_account[id(folder)] = (watch.account_uid, folder_name)

    def _on_folder_changed_signal(
        self, folder: Camel.Folder, _change_info: object
    ) -> None:
        if not self._running:
            return
        located = self._folder_to_account.get(id(folder))
        if located is None:
            return
        account_uid, folder_name = located
        self._schedule_folder_changed(account_uid, folder_name)

    def _schedule_folder_changed(self, account_uid: str, folder_name: str) -> None:
        key = (account_uid, folder_name)
        existing = self._debounce_ids.get(key)
        if existing is not None:
            GLib.source_remove(existing)

        def fire() -> bool:
            self._debounce_ids.pop(key, None)
            if self._running:
                self._on_folder_changed(account_uid, folder_name)
            return False

        timeout_id = GLib.timeout_add(_DEBOUNCE_MS, fire)
        self._debounce_ids[key] = timeout_id

    def _disconnect_folder(self, watch: _AccountWatch, folder_name: str) -> None:
        handler_id = watch.folder_handler_ids.pop(folder_name, None)
        folder = watch.watched_folders.pop(folder_name, None)
        if folder is not None:
            self._folder_to_account.pop(id(folder), None)
            if handler_id is not None:
                folder.disconnect(handler_id)

    def _disconnect_account(self, watch: _AccountWatch) -> None:
        for folder_name in list(watch.watched_folders):
            self._disconnect_folder(watch, folder_name)
        for handler_id in watch.store_handler_ids:
            GObject.signal_handler_disconnect(watch.store, handler_id)
        watch.store_handler_ids.clear()

    def _clear_debounce_timers(self) -> None:
        for timeout_id in self._debounce_ids.values():
            GLib.source_remove(timeout_id)
        self._debounce_ids.clear()
