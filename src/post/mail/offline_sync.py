# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Background download of message bodies into Camel's local cache."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")
from gi.repository import Camel, Gio, GLib

from post.mail.folders import folder_can_contain_messages
from post.mail.offline_settings import (
    account_is_user_offline,
    apply_offline_sync_to_folder,
    downsync_expression_for_mode,
)
from post.preferences import (
    OFFLINE_BODY_SYNC_OFF,
    OfflineBodySyncMode,
    get_account_offline_body_sync,
)

if TYPE_CHECKING:
    from post.mail.eds import MailService

log = logging.getLogger(__name__)

OfflineSyncProgressCallback = Callable[["OfflineSyncProgress"], None]


@dataclass(frozen=True)
class OfflineSyncProgress:
    account_uid: str
    account_label: str
    folder_name: str | None
    active: bool


class OfflineBodySyncCoordinator:
    """Schedules Camel downsync jobs on the mail I/O thread (one account at a time)."""

    def __init__(self, mail: MailService) -> None:
        self._mail = mail
        self._cancellables: dict[str, Gio.Cancellable] = {}
        self._running: set[str] = set()
        self._progress_callbacks: list[OfflineSyncProgressCallback] = []
        self._active_progress: OfflineSyncProgress | None = None

    def add_progress_callback(self, callback: OfflineSyncProgressCallback) -> None:
        self._progress_callbacks.append(callback)
        if self._active_progress is not None:
            callback(self._active_progress)

    def remove_progress_callback(self, callback: OfflineSyncProgressCallback) -> None:
        self._progress_callbacks = [
            item for item in self._progress_callbacks if item is not callback
        ]

    def _notify_progress(self, progress: OfflineSyncProgress | None) -> None:
        self._active_progress = progress
        for callback in list(self._progress_callbacks):
            try:
                callback(progress)
            except Exception:
                log.debug("Offline sync progress callback failed", exc_info=True)

    def schedule_all_accounts(self) -> None:
        for account in self._mail.list_accounts():
            self.schedule_account(account.uid)

    def schedule_account(self, account_uid: str) -> None:
        mode = get_account_offline_body_sync(account_uid)
        if mode == OFFLINE_BODY_SYNC_OFF:
            return
        if account_is_user_offline(account_uid):
            return
        if not self._mail.is_network_available():
            return
        if account_uid in self._running:
            return

        cancellable = self._cancellables.pop(account_uid, None)
        if cancellable is not None:
            cancellable.cancel()

        cancellable = Gio.Cancellable()
        self._cancellables[account_uid] = cancellable
        self._running.add(account_uid)

        def worker() -> None:
            try:
                self._run_account_sync(account_uid, mode, cancellable)
            finally:
                self._running.discard(account_uid)
                self._cancellables.pop(account_uid, None)
                if not self._running:
                    self._notify_progress(None)

        from post.mail.io_thread import get_mail_io_thread

        get_mail_io_thread().submit(worker)

    def cancel_account(self, account_uid: str) -> None:
        cancellable = self._cancellables.get(account_uid)
        if cancellable is not None:
            cancellable.cancel()

    def cancel_all(self) -> None:
        for account_uid in list(self._cancellables):
            self.cancel_account(account_uid)
        if self._running:
            self._notify_progress(None)

    def is_active(self) -> bool:
        return bool(self._running)

    def _run_account_sync(
        self,
        account_uid: str,
        mode: OfflineBodySyncMode,
        cancellable: Gio.Cancellable,
    ) -> None:
        expression = downsync_expression_for_mode(mode)
        if expression is None:
            return

        account = self._mail.get_account(account_uid)
        account_label = account.display_label

        try:
            store = self._mail._get_store_unlocked(account_uid)  # noqa: SLF001
        except Exception:
            log.debug(
                "Skipping offline body sync for unavailable account %s",
                account_uid,
                exc_info=True,
            )
            return

        if not isinstance(store, Camel.OfflineStore):
            return

        if not store.requires_downsync():
            log.debug(
                "Store %s reports no downsync required; running backfill anyway",
                account_uid,
            )

        folders: list[Camel.Folder] = []
        try:
            listed = store.dup_downsync_folders()
            if listed:
                folders.extend(listed)
        except Exception:
            log.debug("dup_downsync_folders failed for %s", account_uid, exc_info=True)

        if not folders:
            try:
                for folder_info in self._mail._list_folders_unlocked(account_uid):  # noqa: SLF001
                    full_name = folder_info.get("full_name")
                    if not isinstance(full_name, str) or not full_name:
                        continue
                    if not folder_can_contain_messages(folder_info):
                        continue
                    folder = store.get_folder_sync(full_name, 0, cancellable)
                    if folder is not None:
                        folders.append(folder)
            except Exception:
                log.debug(
                    "Could not list folders for offline sync on %s",
                    account_uid,
                    exc_info=True,
                )
                return

        for folder in folders:
            if cancellable.is_cancelled():
                break
            if not isinstance(folder, Camel.OfflineFolder):
                continue
            apply_offline_sync_to_folder(folder, mode)
            if not folder.can_downsync():
                continue
            folder_name = folder.get_full_name() or ""
            self._notify_progress(
                OfflineSyncProgress(
                    account_uid=account_uid,
                    account_label=account_label,
                    folder_name=folder_name,
                    active=True,
                )
            )
            self._downsync_folder_sync(folder, expression, cancellable)

    def _downsync_folder_sync(
        self,
        folder: Camel.OfflineFolder,
        expression: str,
        cancellable: Gio.Cancellable,
    ) -> None:
        if cancellable.is_cancelled():
            return
        try:
            folder.downsync_sync(expression, cancellable)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            log.debug(
                "Offline downsync failed for folder %r",
                folder.get_full_name(),
                exc_info=True,
            )
