# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist per-account correspondent indexes for compose autocomplete (#313)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .correspondents import Correspondent

log = logging.getLogger(__name__)

# v2 (#544): usage counts per address and per-folder contributions.
_CACHE_VERSION = 2
# v1 files hold only the address list. They are still read: folder indexes are
# no longer persisted (#513), so this file is the only copy of the history and
# dropping it would leave autocomplete empty until folders are re-indexed.
_LEGACY_VERSION = 1
# Pseudo-folder key under which a v1 address list lives in the per-folder map.
# Its entries carry no counts, so summing them with live folders adds only
# presence/recency and cannot inflate ranking.
LEGACY_FOLDER_KEY = "\x00legacy"
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


def save(
    account_uid: str,
    correspondents: list[Correspondent],
    *,
    folders: dict[str, list[Correspondent]] | None = None,
) -> None:
    """Persist a non-empty correspondent index. Empty lists are not written.

    ``folders`` holds the per-folder contributions the index was aggregated
    from, so later folder updates can replace one contribution instead of
    adding counts a second time.
    """
    if not correspondents:
        return
    path = _cache_path(account_uid)
    payload: dict[str, Any] = {
        "version": _CACHE_VERSION,
        "correspondents": [_correspondent_to_payload(item) for item in correspondents],
    }
    if folders is not None:
        payload["folders"] = {
            name: [_correspondent_to_payload(item) for item in items]
            for name, items in folders.items()
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp_path, path)
    except OSError:
        log.debug(
            "Could not save correspondent cache for %s",
            account_uid,
            exc_info=True,
        )


def _load_payload(account_uid: str) -> dict[str, Any] | None:
    path = _cache_path(account_uid)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.debug(
            "Could not read correspondent cache for %s",
            account_uid,
            exc_info=True,
        )
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") not in (_CACHE_VERSION, _LEGACY_VERSION):
        return None
    return payload


def load(account_uid: str) -> list[Correspondent] | None:
    payload = _load_payload(account_uid)
    if payload is None:
        return None
    correspondents = _correspondents_from_payload(payload.get("correspondents"))
    return correspondents or None


def load_folders(account_uid: str) -> dict[str, list[Correspondent]] | None:
    """Return persisted per-folder contributions, or None when unavailable."""
    payload = _load_payload(account_uid)
    if payload is None:
        return None
    if payload.get("version") == _LEGACY_VERSION:
        legacy = _correspondents_from_payload(payload.get("correspondents"))
        return {LEGACY_FOLDER_KEY: legacy} if legacy else None
    raw_folders = payload.get("folders")
    if not isinstance(raw_folders, dict):
        return None
    folders = {
        name: _correspondents_from_payload(raw_items)
        for name, raw_items in raw_folders.items()
        if isinstance(name, str)
    }
    return folders or None


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
    try:
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
