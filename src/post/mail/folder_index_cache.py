# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Retired folder-index files (#513).

The helper Camel summary is the mailbox. ``load``, ``save``, and ``has_cache``
do not read or write ``~/.cache/post/folder-index``. Existing files stay on
disk until an account is removed.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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
    *,
    grow_only: bool = False,
) -> None:
    """Ignore writes. The Camel summary is the list (#513)."""
    del account_uid, folder_name, messages, unread, total, grow_only


def has_cache(account_uid: str, folder_name: str) -> bool:
    del account_uid, folder_name
    return False


def cached_account_uids() -> list[str]:
    """The folder-index is not consulted (#513)."""
    return []


def cached_folder_names(account_uid: str) -> list[str]:
    """The folder-index is not consulted (#513)."""
    del account_uid
    return []


def load(
    account_uid: str,
    folder_name: str,
) -> tuple[list[dict[str, Any]], int, int] | None:
    """The folder-index is not a mailbox (#513)."""
    del account_uid, folder_name
    return None


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
