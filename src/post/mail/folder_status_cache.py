# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist grow-only STATUS unread/total for heavy folders (#208).

Camel FolderInfo / summary totals for Archive often collapse to the local
summary size (hundreds) after the folder is opened. Keep a high-water mark
from real store FolderInfo REFRESH (STATUS-style) so the sidebar does not
forget a larger server total (tens of thousands) once seen.

Summary-sized observations must never become "the server total."
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_CACHE_ROOT = Path.home() / ".cache" / "post" / "folder-status"

# Totals below this are typical Camel local-summary sizes after opening a
# large M365 Archive. Do not persist them as STATUS, and do not let them
# shrink a larger high-water mark (poisoned REFRESH after folder open).
MIN_TRUSTED_STATUS_TOTAL = 1000


def _cache_path(account_uid: str, folder_name: str) -> Path:
    folder_key = hashlib.sha256(folder_name.encode("utf-8")).hexdigest()[:16]
    return _CACHE_ROOT / account_uid / f"{folder_key}.json"


def load(account_uid: str, folder_name: str) -> tuple[int, int] | None:
    path = _cache_path(account_uid, folder_name)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.debug(
            "Could not read folder STATUS cache for %s / %r",
            account_uid,
            folder_name,
            exc_info=True,
        )
        return None
    if payload.get("version") != _CACHE_VERSION:
        return None
    if payload.get("folder_name") != folder_name:
        return None
    unread = payload.get("unread")
    total = payload.get("total")
    if not isinstance(unread, int) or not isinstance(total, int):
        return None
    if total < 0:
        return None
    return unread, total


def observe(
    account_uid: str,
    folder_name: str,
    unread: int,
    total: int,
    *,
    trusted: bool = False,
) -> tuple[int, int]:
    """Merge a count observation into the high-water STATUS cache.

    ``trusted=True`` means store FolderInfo REFRESH (STATUS-style). Even then,
    summary-sized totals must not shrink a larger high-water (Camel often
    returns local summary size from REFRESH after the folder was opened).

    ``trusted=False`` means Camel summary / folder-index / list without REFRESH:
    only a large total may raise the high-water; nothing may lower it.

    Returns the best (unread, total) known after merging (may still be the
    untrusted input when no high-water exists yet — use :func:`resolve_sidebar`
    before displaying).
    """
    existing = load(account_uid, folder_name)
    if total < 0:
        return existing if existing is not None else (unread, total)

    if existing is None:
        if total >= MIN_TRUSTED_STATUS_TOTAL:
            _save(account_uid, folder_name, unread, total)
            return unread, total
        # Small first observation: never lock in as STATUS.
        return unread, total

    prev_unread, prev_total = existing
    if total > prev_total:
        _save(account_uid, folder_name, unread, total)
        return unread, total
    if total < prev_total:
        # Never shrink with summary-sized / poisoned counts.
        if total < MIN_TRUSTED_STATUS_TOTAL:
            return prev_unread, prev_total
        if trusted:
            _save(account_uid, folder_name, unread, total)
            return unread, total
        return prev_unread, prev_total
    # Same total: prefer fresher unread from trusted STATUS.
    if trusted and unread >= 0 and unread != prev_unread:
        _save(account_uid, folder_name, unread, total)
        return unread, total
    return prev_unread, prev_total


def best(
    account_uid: str, folder_name: str, unread: int, total: int
) -> tuple[int, int]:
    """Return high-water if larger than ``total``, else ``(unread, total)``."""
    existing = load(account_uid, folder_name)
    if existing is None:
        return unread, total
    prev_unread, prev_total = existing
    if prev_total > total:
        return prev_unread, prev_total
    return unread, total


def resolve_sidebar(
    account_uid: str, folder_name: str, unread: int, total: int
) -> tuple[int, int]:
    """Counts safe to show as sidebar STATUS for a heavy folder.

    Prefers a persisted high-water mark. If none exists and ``total`` looks like
    a local Camel summary (below :data:`MIN_TRUSTED_STATUS_TOTAL`), returns
    ``(-1, -1)`` so the UI does not present that figure as the server size.
    """
    existing = load(account_uid, folder_name)
    if existing is not None:
        prev_unread, prev_total = existing
        if total < 0 or prev_total >= total:
            return prev_unread, prev_total
        return unread, total
    if total >= 0 and total < MIN_TRUSTED_STATUS_TOTAL:
        return -1, -1
    return unread, total


def _save(account_uid: str, folder_name: str, unread: int, total: int) -> None:
    path = _cache_path(account_uid, folder_name)
    payload = {
        "version": _CACHE_VERSION,
        "folder_name": folder_name,
        "unread": unread,
        "total": total,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp_path, path)
    except OSError:
        log.debug(
            "Could not save folder STATUS cache for %s / %r",
            account_uid,
            folder_name,
            exc_info=True,
        )
