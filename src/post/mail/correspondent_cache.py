# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist per-account correspondent indexes for compose autocomplete (#313).

The file is the source of truth (#547). The UI and the camel helper both write
it, so a write must never be built on a process's private in-memory copy:
:func:`update_folder` replaces one folder's contribution under a cross-process
lock, starting from what is on disk. Readers tag what they loaded with the
file's :func:`stamp` and re-read only when the stamp changed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .correspondents import Correspondent

log = logging.getLogger(__name__)

# ``(st_mtime_ns, st_size, st_ino)`` of the cache file. mtime alone can miss two
# writes within the filesystem's timestamp granularity. Every write goes through
# ``os.replace`` of a fresh temp file, so size and inode tell such writes apart.
Stamp = tuple[int, int, int]

# v2 (#544): usage counts per address and per-folder contributions.
_CACHE_VERSION = 2
# v1 files (address list only) are ignored (#547). The index is rebuilt from
# the helper's Camel summary as folders are stored, and the first v2 write
# replaces the old file.
_CACHE_ROOT = Path.home() / ".cache" / "post" / "correspondents"


def _cache_path(account_uid: str) -> Path:
    return _CACHE_ROOT / account_uid / "correspondents.json"


def _int_field(raw: dict, key: str) -> int:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _correspondent_to_payload(item: Correspondent) -> dict[str, Any]:
    return {
        "display": item.display,
        "email": item.email,
        "name": item.name,
        "last_seen": item.last_seen,
        "sent_count": item.sent_count,
        "received_count": item.received_count,
        "last_sent": item.last_sent,
        "last_received": item.last_received,
    }


def _correspondent_from_payload(raw: Any) -> Correspondent | None:
    if not isinstance(raw, dict):
        return None
    email = raw.get("email")
    display = raw.get("display")
    if not isinstance(email, str) or not email or "@" not in email:
        return None
    if not isinstance(display, str) or not display:
        display = email
    name = raw.get("name")
    return Correspondent(
        display=display,
        email=email,
        name=name if isinstance(name, str) else "",
        last_seen=_int_field(raw, "last_seen"),
        sent_count=_int_field(raw, "sent_count"),
        received_count=_int_field(raw, "received_count"),
        last_sent=_int_field(raw, "last_sent"),
        last_received=_int_field(raw, "last_received"),
    )


def _correspondents_from_payload(raw_items: Any) -> list[Correspondent]:
    if not isinstance(raw_items, list):
        return []
    items: list[Correspondent] = []
    for raw in raw_items:
        item = _correspondent_from_payload(raw)
        if item is not None:
            items.append(item)
    return items


def _lock_path(account_uid: str) -> Path:
    return _CACHE_ROOT / account_uid / "correspondents.lock"


def _stat_stamp(path: Path) -> Stamp | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def stamp(account_uid: str) -> Stamp | None:
    """Cheap change token of the cache file, or None when it does not exist."""
    return _stat_stamp(_cache_path(account_uid))


@contextmanager
def _locked(account_uid: str) -> Iterator[None]:
    """Hold the per-account cross-process lock for a read-modify-write.

    Plain reads never take it: writes land through ``os.replace``, so a reader
    always sees a complete file. If the lock cannot be taken (for example a
    filesystem without ``flock``), the write proceeds unlocked, which is no
    worse than before the lock existed.
    """
    handle = None
    try:
        lock_path = _lock_path(account_uid)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        log.debug(
            "Could not lock correspondent cache for %s",
            account_uid,
            exc_info=True,
        )
    try:
        yield
    finally:
        if handle is not None:
            handle.close()  # releases the flock


def _payload_dict(
    correspondents: list[Correspondent],
    folders: dict[str, list[Correspondent]] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": _CACHE_VERSION,
        "correspondents": [_correspondent_to_payload(item) for item in correspondents],
    }
    if folders is not None:
        payload["folders"] = {
            name: [_correspondent_to_payload(item) for item in items]
            for name, items in folders.items()
        }
    return payload


def _write_unlocked(
    account_uid: str,
    correspondents: list[Correspondent],
    folders: dict[str, list[Correspondent]] | None,
) -> bool:
    """Atomically write the file. The caller holds the lock. True on success."""
    path = _cache_path(account_uid)
    payload = _payload_dict(correspondents, folders)
    tmp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp_path, path)
    except OSError:
        log.debug(
            "Could not save correspondent cache for %s",
            account_uid,
            exc_info=True,
        )
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def save(
    account_uid: str,
    correspondents: list[Correspondent],
    *,
    folders: dict[str, list[Correspondent]] | None = None,
    if_absent: bool = False,
) -> Stamp | None:
    """Persist a non-empty correspondent index. Empty lists are not written.

    ``folders`` holds the per-folder contributions the index was aggregated
    from, so later folder updates can replace one contribution instead of
    adding counts a second time.

    ``if_absent`` is for bootstrapping from local folder indexes: another
    process may have written the file meanwhile, and that file wins.

    Returns the stamp of the file written, or None when nothing was written.
    """
    if not correspondents:
        return None
    with _locked(account_uid):
        if if_absent and stamp(account_uid) is not None:
            return None
        if not _write_unlocked(account_uid, correspondents, folders):
            return None
        return stamp(account_uid)


@dataclass(frozen=True)
class Snapshot:
    """One consistent read of the cache file."""

    correspondents: list[Correspondent]
    # None when the file carries no per-folder contributions.
    folders: dict[str, list[Correspondent]] | None
    stamp: Stamp


def _read_payload(account_uid: str) -> tuple[dict[str, Any], Stamp] | None:
    """Parse the file and stamp the very inode that was read."""
    path = _cache_path(account_uid)
    try:
        with path.open(encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.debug(
            "Could not read correspondent cache for %s",
            account_uid,
            exc_info=True,
        )
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _CACHE_VERSION:
        return None
    return payload, (info.st_mtime_ns, info.st_size, info.st_ino)


def _folders_from_payload(
    payload: dict[str, Any],
) -> dict[str, list[Correspondent]] | None:
    raw_folders = payload.get("folders")
    if not isinstance(raw_folders, dict):
        return None
    folders = {
        name: _correspondents_from_payload(raw_items)
        for name, raw_items in raw_folders.items()
        if isinstance(name, str)
    }
    return folders or None


def load_snapshot(account_uid: str) -> Snapshot | None:
    """Return the cache contents together with the stamp they were read at.

    None when the file is missing, unreadable, of another version, or holds no
    correspondents.
    """
    read = _read_payload(account_uid)
    if read is None:
        return None
    payload, file_stamp = read
    correspondents = _correspondents_from_payload(payload.get("correspondents"))
    if not correspondents:
        return None
    return Snapshot(
        correspondents=correspondents,
        folders=_folders_from_payload(payload),
        stamp=file_stamp,
    )


def load(account_uid: str) -> list[Correspondent] | None:
    snapshot = load_snapshot(account_uid)
    return snapshot.correspondents if snapshot is not None else None


def load_folders(account_uid: str) -> dict[str, list[Correspondent]] | None:
    """Return persisted per-folder contributions, or None when unavailable."""
    read = _read_payload(account_uid)
    if read is None:
        return None
    return _folders_from_payload(read[0])


def update_folder(
    account_uid: str,
    folder_name: str,
    items: list[Correspondent],
    aggregate: Callable[[Iterable[list[Correspondent]]], list[Correspondent]],
    *,
    known: tuple[Stamp | None, dict[str, list[Correspondent]]] | None = None,
) -> tuple[list[Correspondent], dict[str, list[Correspondent]], Stamp | None]:
    """Replace one folder's contribution, based on what is on disk (#547).

    Under the cross-process lock this reads the file's current per-folder
    map, replaces ``folder_name``, re-aggregates and writes atomically. Doing
    it from the file rather than from a caller's in-memory copy means a stale
    UI index can never overwrite what the helper wrote, and vice versa.

    ``known`` is an optimisation: a caller's ``(stamp, folders)`` pair from an
    earlier read or write. If the file's stamp still equals it (checked under
    the lock, so nobody can change the file meanwhile), parsing is skipped and
    ``folders`` is used as the base. Otherwise the file is parsed.

    Returns ``(merged, folders, stamp)``. ``stamp`` is the stamp of the file
    after the write (or the current one when nothing was written).
    """
    with _locked(account_uid):
        if known is not None and known[0] == stamp(account_uid):
            folders = dict(known[1])
        else:
            read = _read_payload(account_uid)
            folders = (
                _folders_from_payload(read[0]) if read is not None else None
            ) or {}
        folders[folder_name] = list(items)
        merged = aggregate(folders.values())
        if merged:
            _write_unlocked(account_uid, merged, folders)
        return merged, folders, stamp(account_uid)


def cached_account_uids() -> list[str]:
    """Return account uids that have a correspondents directory on disk."""
    if not _CACHE_ROOT.is_dir():
        return []
    try:
        return [path.name for path in _CACHE_ROOT.iterdir() if path.is_dir()]
    except OSError:
        return []


def invalidate(account_uid: str) -> None:
    path = _cache_path(account_uid)
    if not path.parent.is_dir():
        return  # nothing cached; do not create a directory just to lock it
    try:
        with _locked(account_uid):
            path.unlink(missing_ok=True)
    except OSError:
        log.debug(
            "Could not invalidate correspondent cache for %s",
            account_uid,
            exc_info=True,
        )


def invalidate_account(account_uid: str) -> None:
    account_dir = _CACHE_ROOT / account_uid
    if not account_dir.is_dir():
        return
    try:
        shutil.rmtree(account_dir)
    except OSError:
        log.debug(
            "Could not invalidate correspondent cache for account %s",
            account_uid,
            exc_info=True,
        )
