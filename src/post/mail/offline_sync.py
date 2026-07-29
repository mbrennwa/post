# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Background download of message bodies into Camel's local cache."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")
from gi.repository import Camel, Gio, GLib

from post.mail.folders import folder_can_contain_messages
from post.mail.io_thread import get_mail_io_thread
from post.mail.message_list_state import (
    is_heavy_folder_name,
    offline_folder_priority,
)
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

# Bound downsync_sync so one folder cannot pin post-mail-io for minutes (#197).
_OFFLINE_DOWNSYNC_TIMEOUT_SECONDS = 30

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

        get_mail_io_thread().submit_background(
            self._account_sync_worker, account_uid, mode, cancellable
        )

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

    def _account_sync_worker(
        self,
        account_uid: str,
        mode: OfflineBodySyncMode,
        cancellable: Gio.Cancellable,
        *,
        folders: list[Camel.Folder] | None = None,
        folder_index: int = 0,
    ) -> None:
        # Respect interactive Archive/folder holds: do not resume body backfill
        # while the UI has paused offline sync (#208).
        if self._mail.offline_body_sync_is_held():
            self._running.discard(account_uid)
            self._cancellables.pop(account_uid, None)
            if not self._running:
                self._notify_progress(None)
            return
        complete = False
        try:
            complete = self._run_account_sync(
                account_uid,
                mode,
                cancellable,
                folders=folders,
                folder_index=folder_index,
            )
        finally:
            if complete:
                self._running.discard(account_uid)
                self._cancellables.pop(account_uid, None)
                if not self._running:
                    self._notify_progress(None)

    def _run_account_sync(
        self,
        account_uid: str,
        mode: OfflineBodySyncMode,
        cancellable: Gio.Cancellable,
        *,
        folders: list[Camel.Folder] | None = None,
        folder_index: int = 0,
    ) -> bool:
        expression = downsync_expression_for_mode(mode)
        if expression is None:
            return True

        account = self._mail.get_account(account_uid)
        account_label = account.display_label

        if folders is None:
            folders = self._collect_downsync_folders(account_uid, cancellable)
            if folders is None:
                return True
            folder_index = 0

        while folder_index < len(folders):
            if cancellable.is_cancelled():
                return True
            if self._mail.offline_body_sync_is_held():
                return True
            if get_mail_io_thread().has_interactive_work_pending():
                get_mail_io_thread().submit_background(
                    self._account_sync_worker,
                    account_uid,
                    mode,
                    cancellable,
                    folders=folders,
                    folder_index=folder_index,
                )
                return False

            folder = folders[folder_index]
            folder_index += 1
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
            # Do not call continue_heavy_folder_index with refresh here: M365
            # refresh_info can pin post-mail-io (#208). Index local summary only
            # after body downsync so the list tracks newly cached headers.
            self._downsync_folder_sync(folder, expression, cancellable)
            if is_heavy_folder_name(folder_name) and not cancellable.is_cancelled():
                try:
                    self._mail.continue_heavy_folder_index(
                        account_uid,
                        folder_name,
                        allow_refresh=False,
                    )
                except Exception:
                    log.debug(
                        "Post-downsync local index failed for %r",
                        folder_name,
                        exc_info=True,
                    )

        return True

    def _collect_downsync_folders(
        self,
        account_uid: str,
        cancellable: Gio.Cancellable,
    ) -> list[Camel.Folder] | None:
        try:
            store = self._mail._get_store_unlocked(account_uid)  # noqa: SLF001
        except Exception:
            log.debug(
                "Skipping offline body sync for unavailable account %s",
                account_uid,
                exc_info=True,
            )
            return None

        if not isinstance(store, Camel.OfflineStore):
            return None

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
                return None

        return self._sort_folders_by_offline_priority(folders)

    @staticmethod
    def _sort_folders_by_offline_priority(
        folders: list[Camel.Folder],
    ) -> list[Camel.Folder]:
        """Ordinary → Archive → Trash → Junk (#208)."""

        def sort_key(folder: Camel.Folder) -> tuple[int, str]:
            name = folder.get_full_name() or ""
            try:
                flags = int(folder.get_flags())
            except Exception:
                flags = 0
            priority = offline_folder_priority(
                name,
                folder_flags=flags,
                type_archive=int(Camel.FolderInfoFlags.TYPE_ARCHIVE),
                type_trash=int(Camel.FolderInfoFlags.TYPE_TRASH),
                type_junk=int(Camel.FolderInfoFlags.TYPE_JUNK),
            )
            return (priority, name.lower())

        return sorted(folders, key=sort_key)

    def _downsync_folder_sync(
        self,
        folder: Camel.OfflineFolder,
        expression: str,
        account_cancellable: Gio.Cancellable,
    ) -> None:
        """Downsync one folder; timeout cancels only this folder (#208)."""
        if account_cancellable.is_cancelled():
            return
        folder_name = folder.get_full_name() or ""
        chunk_cancellable = Gio.Cancellable()
        stop_watch = threading.Event()

        def _watch_account_and_timeout() -> None:
            deadline = time.monotonic() + _OFFLINE_DOWNSYNC_TIMEOUT_SECONDS
            while not stop_watch.is_set():
                if account_cancellable.is_cancelled():
                    chunk_cancellable.cancel()
                    return
                if time.monotonic() >= deadline:
                    chunk_cancellable.cancel()
                    return
                stop_watch.wait(0.05)

        watcher = threading.Thread(
            target=_watch_account_and_timeout,
            name="post-offline-downsync-watch",
            daemon=True,
        )
        started = time.monotonic()
        watcher.start()
        try:
            folder.downsync_sync(expression, chunk_cancellable)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                elapsed = time.monotonic() - started
                if account_cancellable.is_cancelled():
                    log.debug(
                        "Offline downsync cancelled for folder %r after %.1fs",
                        folder_name,
                        elapsed,
                    )
                elif elapsed >= _OFFLINE_DOWNSYNC_TIMEOUT_SECONDS * 0.9:
                    log.warning(
                        "Offline downsync timed out after %.1fs for folder %r "
                        "(continuing with next folder)",
                        elapsed,
                        folder_name,
                    )
                else:
                    log.debug(
                        "Offline downsync cancelled for folder %r after %.1fs",
                        folder_name,
                        elapsed,
                    )
                return
            log.debug(
                "Offline downsync failed for folder %r",
                folder_name,
                exc_info=True,
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=1.0)
