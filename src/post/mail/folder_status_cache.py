# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist grow-only STATUS unread/total for heavy folders (#208).

Camel FolderInfo / summary totals for Archive often collapse to the local
summary size (hundreds–low thousands) after the folder is opened. Keep a
high-water mark from real server counts (Graph ``totalItemCount`` / STATUS-style
FolderInfo) so the sidebar does not forget a larger server total (tens of
thousands) once seen.

Summary-sized observations must never become "the server total."
High-water marks only rise — never shrink — so a poisoned REFRESH of ~1300
cannot overwrite a prior ~28k STATUS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from post.mail.message_list_state import is_trash_or_junk_folder_name

log = logging.getLogger(__name__)

_CACHE_VERSION = 3
_CACHE_ROOT = Path.home() / ".cache" / "post" / "folder-status"

# Totals below this are typical Camel local-summary sizes after opening a
# large M365 Archive. Do not persist them as Archive STATUS. Trash/Junk are
# often legitimately smaller and use a separate lock-in path (#208).
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


def clear(account_uid: str, folder_name: str) -> None:
    """Drop a poisoned STATUS high-water entry."""
    path = _cache_path(account_uid, folder_name)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug(
            "Could not clear folder STATUS cache for %s / %r",
            account_uid,
            folder_name,
            exc_info=True,
        )


def looks_like_summary_echo(total: int, local_indexed: int) -> bool:
    """True when ``total`` is basically the local Camel/index size, not STATUS.

    Real Archive STATUS is tens of thousands. Poisoned values track the growing
    local summary (~1k–2k). Exact catch-up at a large trusted total is not an echo.
    """
    if local_indexed < 0 or total < 0:
        return False
    if total < MIN_TRUSTED_STATUS_TOTAL:
        return True
    close = abs(total - local_indexed) <= 100 or (
        local_indexed >= MIN_TRUSTED_STATUS_TOTAL
        and total <= int(local_indexed * 1.05) + 50
    )
    # Large equal totals (true catch-up to Graph) are not summary echoes.
    if close and total < 10_000:
        return True
    return False


def scrub_if_summary_echo(
    account_uid: str, folder_name: str, local_indexed: int
) -> None:
    """Remove a high-water mark that only echoes the local index (#208)."""
    # Trash/Junk STATUS is often <1000; the Archive echo heuristic must not
    # wipe those legitimate totals.
    if is_trash_or_junk_folder_name(folder_name):
        return
    existing = load(account_uid, folder_name)
    if existing is None:
        return
    _unread, total = existing
    if looks_like_summary_echo(total, local_indexed):
        log.info(
            "Clearing poisoned STATUS for %s/%s (cached=%d local_indexed=%d)",
            account_uid,
            folder_name,
            total,
            local_indexed,
        )
        clear(account_uid, folder_name)


def observe(
    account_uid: str,
    folder_name: str,
    unread: int,
    total: int,
    *,
    trusted: bool = False,
    local_indexed: int | None = None,
) -> tuple[int, int]:
    """Merge a count observation into the high-water STATUS cache.

    ``trusted=True`` means Graph ``totalItemCount`` or a STATUS-style FolderInfo
    count that is not a local-summary echo. Summary-sized / index-echo totals
    must never become the first STATUS lock-in, and nothing may lower a larger
    high-water.

    Returns the best (unread, total) known after merging (may still be the
    untrusted input when no high-water exists yet — use :func:`resolve_sidebar`
    before displaying).
    """
    existing = load(account_uid, folder_name)
    if total < 0:
        return existing if existing is not None else (unread, total)

    if existing is None:
        if not trusted:
            return unread, total
        # Trash/Junk: trusted STATUS may be small; persist it so the sidebar
        # can show counts / drop "(working…)" instead of a permanent blank.
        if is_trash_or_junk_folder_name(folder_name):
            _save(account_uid, folder_name, unread, total)
            return unread, total
        # Archive / All Mail: refuse summary-sized first lock-in.
        if total < MIN_TRUSTED_STATUS_TOTAL:
            return unread, total
        if local_indexed is not None and looks_like_summary_echo(
            total, local_indexed
        ):
            return unread, total
        _save(account_uid, folder_name, unread, total)
        return unread, total

    prev_unread, prev_total = existing
    if total > prev_total:
        if (
            not is_trash_or_junk_folder_name(folder_name)
            and local_indexed is not None
            and looks_like_summary_echo(total, local_indexed)
        ):
            # Do not raise the high-water with a summary echo.
            return prev_unread, prev_total
        _save(account_uid, folder_name, unread, total)
        return unread, total
    if total < prev_total:
        # Grow-only: never shrink (poisoned REFRESH of 1300 must not beat 28k).
        return prev_unread, prev_total
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

    Prefers a persisted high-water mark (created only by trusted non-echo
    observations). Without a high-water, returns ``(-1, -1)`` so the UI does
    not present Camel summary sizes as the server size.
    """
    existing = load(account_uid, folder_name)
    if existing is not None:
        prev_unread, prev_total = existing
        if total < 0 or prev_total >= total:
            return prev_unread, prev_total
        return unread, total
    return -1, -1


def index_caught_up(
    indexed: int,
    server_total: int,
    folder_name: str | None = None,
) -> bool:
    """True when heavy-folder header index has caught a trusted STATUS (#208).

    Unknown / summary-sized Archive STATUS must not count as catch-up — that
    froze Archive at ~1300 while the server still had ~28k. Trash/Junk STATUS
    is often legitimately under 1000 once locked in.
    """
    if not status_total_is_trusted(folder_name, server_total):
        return False
    if folder_name and is_trash_or_junk_folder_name(folder_name):
        return indexed >= server_total
    if looks_like_summary_echo(server_total, indexed):
        return False
    return indexed >= server_total


def status_total_is_trusted(folder_name: str | None, server_total: int) -> bool:
    """True when ``server_total`` is safe to show as STATUS / chase toward.

    Archive requires a large lock-in; Trash/Junk may be small.
    """
    if server_total < 0:
        return False
    if is_trash_or_junk_folder_name(folder_name):
        return True
    return server_total >= MIN_TRUSTED_STATUS_TOTAL


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
