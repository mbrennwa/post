# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist per-account correspondent indexes for compose autocomplete (#313)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .correspondents import Correspondent

log = logging.getLogger(__name__)

_CACHE_VERSION = 1
_CACHE_ROOT = Path.home() / ".cache" / "post" / "correspondents"


def _cache_path(account_uid: str) -> Path:
    return _CACHE_ROOT / account_uid / "correspondents.json"


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
    last_seen = raw.get("last_seen")
    return Correspondent(
        display=display,
        email=email,
        name=name if isinstance(name, str) else "",
        last_seen=last_seen if isinstance(last_seen, int) else 0,
    )


def save(account_uid: str, correspondents: list[Correspondent]) -> None:
    """Persist a non-empty correspondent index. Empty lists are not written."""
    if not correspondents:
        return
    path = _cache_path(account_uid)
    payload = {
        "version": _CACHE_VERSION,
        "correspondents": [
            {
                "display": item.display,
                "email": item.email,
                "name": item.name,
                "last_seen": item.last_seen,
            }
            for item in correspondents
        ],
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


def load(account_uid: str) -> list[Correspondent] | None:
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
    if payload.get("version") != _CACHE_VERSION:
        return None
    raw_items = payload.get("correspondents")
    if not isinstance(raw_items, list):
        return None
    correspondents: list[Correspondent] = []
    for raw in raw_items:
        item = _correspondent_from_payload(raw)
        if item is not None:
            correspondents.append(item)
    if not correspondents:
        return None
    return correspondents


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
