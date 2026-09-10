# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Background download of message bodies into Camel's local cache."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")
from gi.repository import Camel, Gio, GLib

from post.mail.camel_util import camel_uid_is_binary, camel_uid_to_api, folder_get_message_info
from post.mail.folders import folder_can_contain_messages, is_post_outbox_folder
from post.mail.io_thread import get_mail_io_thread
from post.mail.message_list_state import (
    is_heavy_folder_name,
    offline_folder_priority,
)
from post.mail.offline_settings import (
    account_is_user_offline,
    apply_offline_sync_to_folder,
    downsync_expression_for_mode,
    message_within_offline_age,
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
# Per-UID arrival FETCH (#372). Short so interactive mail I/O is not pinned.
ARRIVAL_PREFETCH_BURST = 20
_ARRIVAL_PREFETCH_TIMEOUT_SECONDS = 15

OfflineSyncProgressCallback = Callable[["OfflineSyncProgress"], None]


def added_uids_from_change_info(change_info: object) -> list[str]:
    """Return UIDs Camel reports as newly added on ``Folder::changed``."""
    getter = getattr(change_info, "get_added_uids", None)
    if not callable(getter):
        return []
    try:
        raw = getter()
    except Exception:
        return []
    if not raw:
        return []
    added: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            added.append(item)
    return added


def select_arrival_prefetch_uids(
    uids: list[str],
    *,
    sort_dates: dict[str, int] | None = None,
    limit: int = ARRIVAL_PREFETCH_BURST,
) -> list[str]:
    """Deduplicate *uids* and keep at most *limit*, preferring newest dates."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for uid in uids:
        if not uid or uid in seen_set:
            continue
        seen_set.add(uid)
        seen.append(uid)
    if len(seen) <= limit:
        return seen
    if sort_dates:
        return sorted(
            seen,
            key=lambda uid: sort_dates.get(uid, 0),
            reverse=True,
        )[:limit]
    return seen[-limit:]


def sort_dates_for_uids(folder: object, uids: list[str]) -> dict[str, int]:
    """Map UIDs to Camel MessageInfo received/sent timestamps when available."""
    dates: dict[str, int] = {}
    for uid in uids:
        info = folder_get_message_info(folder, uid)
        if info is None:
            continue
        getter = getattr(info, "get_date_received", None)
        sent_getter = getattr(info, "get_date_sent", None)
        raw = getter() if callable(getter) else None
        if not isinstance(raw, (int, float)) or raw <= 0:
            raw = sent_getter() if callable(sent_getter) else None
        if isinstance(raw, (int, float)) and raw > 0:
            dates[uid] = int(raw)
    return dates


@dataclass(frozen=True)
class _ArrivalPrefetchItem:
    account_uid: str
    folder_name: str
    uid: str


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
        self._arrival_queue: deque[_ArrivalPrefetchItem] = deque()
        self._arrival_queued: set[tuple[str, str, str]] = set()
        self._arrival_running = False
        self._arrival_fetch_cancellable: Gio.Cancellable | None = None
        self._arrival_lock = threading.Lock()

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
        """Cancel full-account downsync only. Arrival prefetch keeps its queue."""
        for account_uid in list(self._cancellables):
            self.cancel_account(account_uid)
        if self._running:
            self._notify_progress(None)

    def cancel_arrival_in_flight(self) -> None:
        """Cancel the current per-UID FETCH so interactive I/O can run (#372)."""
        cancellable = self._arrival_fetch_cancellable
        if cancellable is not None:
            cancellable.cancel()

    def resume_arrival_prefetch(self) -> None:
        """Re-queue the arrival worker after interactive I/O finishes."""
        with self._arrival_lock:
            if self._arrival_running or not self._arrival_queue:
                return
            self._arrival_running = True
        get_mail_io_thread().submit_background(self._arrival_prefetch_worker)

    def schedule_arrival_prefetch(
        self,
        account_uid: str,
        folder_name: str,
        uids: list[str],
        *,
        sort_dates: dict[str, int] | None = None,
    ) -> None:
        """Queue best-effort MIME fetches for newly arrived UIDs (#372).

        Ignores the Archive/heavy-folder hold used by full-account downsync.
        """
        if not uids:
            return
        if is_post_outbox_folder(folder_name):
            return
        mode = get_account_offline_body_sync(account_uid)
        if mode == OFFLINE_BODY_SYNC_OFF:
            return
        if account_is_user_offline(account_uid):
            return
        if not self._mail.is_network_available():
            return
        selected = select_arrival_prefetch_uids(uids, sort_dates=sort_dates)
        if not selected:
            return
        start_worker = False
        with self._arrival_lock:
            folder_queued = sum(
                1
                for item in self._arrival_queue
                if item.account_uid == account_uid
                and item.folder_name == folder_name
            )
            room = ARRIVAL_PREFETCH_BURST - folder_queued
            for uid in selected[: max(0, room)]:
                key = (account_uid, folder_name, uid)
                if key in self._arrival_queued:
                    continue
                self._arrival_queued.add(key)
                self._arrival_queue.append(
                    _ArrivalPrefetchItem(account_uid, folder_name, uid)
                )
            if not self._arrival_running and self._arrival_queue:
                self._arrival_running = True
                start_worker = True
        if start_worker:
            get_mail_io_thread().submit_background(self._arrival_prefetch_worker)

    def is_active(self) -> bool:
        return bool(self._running)

    def _pop_arrival_item(self) -> _ArrivalPrefetchItem | None:
        with self._arrival_lock:
            if not self._arrival_queue:
                return None
            item = self._arrival_queue.popleft()
            self._arrival_queued.discard(
                (item.account_uid, item.folder_name, item.uid)
            )
            return item

    def _requeue_arrival_item(self, item: _ArrivalPrefetchItem) -> None:
        key = (item.account_uid, item.folder_name, item.uid)
        with self._arrival_lock:
            if key in self._arrival_queued:
                return
            self._arrival_queued.add(key)
            self._arrival_queue.appendleft(item)

    def _finish_arrival_worker(self, *, resubmit: bool) -> None:
        with self._arrival_lock:
            has_work = bool(self._arrival_queue)
            if resubmit and has_work:
                self._arrival_running = True
            else:
                self._arrival_running = False
                has_work = False
        if has_work:
            get_mail_io_thread().submit_background(self._arrival_prefetch_worker)

    def _arrival_prefetch_worker(self) -> None:
        try:
            while True:
                if get_mail_io_thread().has_interactive_work_pending():
                    self._finish_arrival_worker(resubmit=True)
                    return
                item = self._pop_arrival_item()
                if item is None:
                    self._finish_arrival_worker(resubmit=False)
                    return
                mode = get_account_offline_body_sync(item.account_uid)
                if (
                    mode == OFFLINE_BODY_SYNC_OFF
                    or account_is_user_offline(item.account_uid)
                    or not self._mail.is_network_available()
                ):
                    continue
                try:
                    folder = self._mail._open_folder_unlocked(  # noqa: SLF001
                        item.account_uid, item.folder_name
                    )
                except Exception:
                    log.debug(
                        "Arrival prefetch skipped open for %s/%s",
                        item.account_uid,
                        item.folder_name,
                        exc_info=True,
                    )
                    continue
                if folder is None:
                    continue
                apply_offline_sync_to_folder(folder, mode)
                if camel_uid_is_binary(item.uid):
                    continue
                try:
                    api_uid = camel_uid_to_api(item.uid)
                except TypeError:
                    continue
                if self._arrival_uid_is_cached(folder, api_uid):
                    continue
                if not self._arrival_uid_in_age_window(folder, item.uid, mode):
                    continue
                preempted = self._prefetch_one_uid(folder, api_uid)
                if preempted:
                    self._requeue_arrival_item(item)
                    self._finish_arrival_worker(resubmit=True)
                    return
        except Exception:
            log.debug("Arrival body prefetch worker failed", exc_info=True)
            self._finish_arrival_worker(resubmit=bool(self._arrival_queue))

    def _arrival_uid_is_cached(self, folder: Camel.Folder, api_uid: str) -> bool:
        try:
            return self._mail._first_cached_rfc822_path(folder, api_uid) is not None  # noqa: SLF001
        except Exception:
            log.debug("Arrival cache probe failed for %s", api_uid, exc_info=True)
            return False

    @staticmethod
    def _arrival_uid_in_age_window(
        folder: Camel.Folder,
        uid: str,
        mode: OfflineBodySyncMode,
    ) -> bool:
        info = folder_get_message_info(folder, uid)
        if info is None:
            return message_within_offline_age(mode, None)
        getter = getattr(info, "get_date_received", None)
        sent_getter = getattr(info, "get_date_sent", None)
        raw = getter() if callable(getter) else None
        if not isinstance(raw, (int, float)) or raw <= 0:
            raw = sent_getter() if callable(sent_getter) else None
        return message_within_offline_age(mode, raw)

    def _prefetch_one_uid(self, folder: Camel.Folder, api_uid: str) -> bool:
        """Fetch one MIME. Return True when interactive preempt cancelled the FETCH."""
        fetch_cancellable = Gio.Cancellable()
        stop_watch = threading.Event()
        self._arrival_fetch_cancellable = fetch_cancellable

        def _watch_timeout() -> None:
            deadline = time.monotonic() + _ARRIVAL_PREFETCH_TIMEOUT_SECONDS
            while not stop_watch.is_set():
                if fetch_cancellable.is_cancelled():
                    return
                if time.monotonic() >= deadline:
                    fetch_cancellable.cancel()
                    return
                stop_watch.wait(0.05)

        watcher = threading.Thread(
            target=_watch_timeout,
            name="post-arrival-prefetch-watch",
            daemon=True,
        )
        started = time.monotonic()
        watcher.start()
        try:
            folder.synchronize_message_sync(api_uid, fetch_cancellable)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                elapsed = time.monotonic() - started
                if get_mail_io_thread().has_interactive_work_pending():
                    log.debug(
                        "Arrival prefetch preempted for %s after %.1fs",
                        api_uid,
                        elapsed,
                    )
                    return True
                if elapsed >= _ARRIVAL_PREFETCH_TIMEOUT_SECONDS * 0.9:
                    log.debug(
                        "Arrival prefetch timed out after %.1fs for %s",
                        elapsed,
                        api_uid,
                    )
                return False
            log.debug(
                "Arrival prefetch failed for %s",
                api_uid,
                exc_info=True,
            )
        except Exception:
            log.debug(
                "Arrival prefetch failed for %s",
                api_uid,
                exc_info=True,
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=1.0)
            if self._arrival_fetch_cancellable is fetch_cancellable:
                self._arrival_fetch_cancellable = None
        return False

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
