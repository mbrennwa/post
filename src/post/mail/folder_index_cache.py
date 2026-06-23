# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist folder message index metadata for fast folder opens."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_CACHE_ROOT = Path.home() / ".cache" / "post" / "folder-index"


def _cache_path(account_uid: str, folder_name: str) -> Path:
    folder_key = hashlib.sha256(folder_name.encode("utf-8")).hexdigest()[:16]
    return _CACHE_ROOT / account_uid / f"{folder_key}.json"


def save(
    account_uid: str,
    folder_name: str,
    messages: list[dict[str, Any]],
    unread: int,
    total: int,
) -> None:
    path = _cache_path(account_uid, folder_name)
    payload = {
        "version": _CACHE_VERSION,
        "folder_name": folder_name,
        "unread": unread,
        "total": total,
        "messages": messages,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp_path, path)
    except OSError:
        log.debug(
            "Could not save folder index cache for %s / %r",
            account_uid,
            folder_name,
            exc_info=True,
        )


def has_cache(account_uid: str, folder_name: str) -> bool:
    return _cache_path(account_uid, folder_name).is_file()


def load(
    account_uid: str,
    folder_name: str,
) -> tuple[list[dict[str, Any]], int, int] | None:
    path = _cache_path(account_uid, folder_name)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.debug(
            "Could not read folder index cache for %s / %r",
            account_uid,
            folder_name,
            exc_info=True,
        )
        return None

    if payload.get("version") != _CACHE_VERSION:
        return None
    if payload.get("folder_name") != folder_name:
        return None

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None

    unread = payload.get("unread")
    total = payload.get("total")
    if not isinstance(unread, int) or not isinstance(total, int):
        return None

    return messages, unread, total


def invalidate(account_uid: str, folder_name: str) -> None:
    path = _cache_path(account_uid, folder_name)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug(
            "Could not invalidate folder index cache for %s / %r",
            account_uid,
            folder_name,
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
            "Could not invalidate folder index cache for account %s",
            account_uid,
            exc_info=True,
        )
