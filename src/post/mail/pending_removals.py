# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Durable UIDs hidden from a folder until their queued MOVE flush finishes (#503/#502).

Camel ``FolderSummary`` must keep those UIDs until ``transfer_messages_to_sync``
runs, so list/index rebuilds subtract this set instead of pruning Camel early.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from typing import Iterable

log = logging.getLogger(__name__)


def pending_removals_dir() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".config", "post", "pending-removals"
    )


def _folder_key(folder_name: str) -> str:
    return hashlib.sha256(folder_name.encode("utf-8")).hexdigest()[:16]


def _safe_account_dir(account_uid: str) -> str:
    return account_uid.replace("/", "_").replace("\0", "_")


def _path(account_uid: str, folder_name: str) -> str:
    return os.path.join(
        pending_removals_dir(),
        _safe_account_dir(account_uid),
        f"{_folder_key(folder_name)}.json",
    )


def _normalize_uids(message_uids: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in message_uids:
        text = str(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def load_pending_uids(account_uid: str, folder_name: str) -> set[str]:
    path = _path(account_uid, folder_name)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.warning("Ignoring corrupt pending-removals file %s", path, exc_info=True)
        return set()
    if not isinstance(data, dict):
        return set()
    uids = data.get("uids") or []
    if not isinstance(uids, list):
        return set()
    return {str(uid) for uid in uids if uid}


def _write_uids(account_uid: str, folder_name: str, uids: set[str]) -> None:
    path = _path(account_uid, folder_name)
    directory = os.path.dirname(path)
    if not uids:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
        return
    os.makedirs(directory, exist_ok=True)
    payload = {
        "account_uid": account_uid,
        "folder_name": folder_name,
        "uids": sorted(uids),
    }
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def note_pending_uids(
    account_uid: str, folder_name: str, message_uids: Iterable[str]
) -> set[str]:
    """Add UIDs and return the full pending set for the folder."""
    current = load_pending_uids(account_uid, folder_name)
    current.update(_normalize_uids(message_uids))
    _write_uids(account_uid, folder_name, current)
    return set(current)


def clear_pending_uids(
    account_uid: str, folder_name: str, message_uids: Iterable[str]
) -> set[str]:
    """Remove UIDs and return the remaining pending set for the folder."""
    current = load_pending_uids(account_uid, folder_name)
    if not current:
        return set()
    for uid in _normalize_uids(message_uids):
        current.discard(uid)
    _write_uids(account_uid, folder_name, current)
    return set(current)


def load_all_pending_removals() -> dict[tuple[str, str], set[str]]:
    """Load every pending-removal file into ``(account, folder) → uids``."""
    root = pending_removals_dir()
    if not os.path.isdir(root):
        return {}
    loaded: dict[tuple[str, str], set[str]] = {}
    try:
        account_names = os.listdir(root)
    except OSError:
        return {}
    for account_name in account_names:
        account_dir = os.path.join(root, account_name)
        if not os.path.isdir(account_dir):
            continue
        try:
            names = os.listdir(account_dir)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(account_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            account_uid = str(data.get("account_uid") or "")
            folder_name = str(data.get("folder_name") or "")
            uids = data.get("uids") or []
            if not account_uid or not folder_name or not isinstance(uids, list):
                continue
            key = (account_uid, folder_name)
            bucket = loaded.setdefault(key, set())
            for uid in uids:
                text = str(uid)
                if text:
                    bucket.add(text)
    return loaded
