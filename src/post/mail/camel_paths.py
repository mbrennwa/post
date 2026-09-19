# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Resolve Camel ``user_data`` / ``user_cache`` directories (#445 / #422 Phase 3b).

Helper processes must not share ``~/.local/share/evolution`` — multi-process
Camel on one store caused mid-read exits (#439). Each helper gets private dirs
under the Post cache tree. System Evolution locations remain for non-helper
tools and legacy path resolution.
"""

from __future__ import annotations

import os
import re

ENV_DATA = "POST_MAIL_CAMEL_USER_DATA"
ENV_CACHE = "POST_MAIL_CAMEL_USER_CACHE"

_SAFE_UID = re.compile(r"[^A-Za-z0-9._-]+")


def default_evolution_dirs() -> tuple[str, str]:
    """System Evolution Camel locations (non-helper / legacy fallback).

    Product helpers use ``helper_camel_dirs`` instead. These paths remain for
    ``resolve_camel_dirs`` when not in a helper process (tests, rare fallbacks).
    """
    return (
        os.path.expanduser("~/.local/share/evolution"),
        os.path.expanduser("~/.cache/evolution"),
    )


def _safe_account_uid(account_uid: str) -> str:
    cleaned = _SAFE_UID.sub("_", (account_uid or "unknown").strip()) or "unknown"
    return cleaned[:120]


def helper_camel_root(account_uid: str) -> str:
    """Root for one account's private Camel data+cache (#445)."""
    xdg_cache = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    cache_home = xdg_cache or os.path.expanduser("~/.cache")
    return os.path.join(cache_home, "post", "camel-helper", _safe_account_uid(account_uid))


def helper_camel_dirs(account_uid: str) -> tuple[str, str]:
    root = helper_camel_root(account_uid)
    return os.path.join(root, "data"), os.path.join(root, "cache")


def configure_helper_camel_dirs(account_uid: str) -> tuple[str, str]:
    """Create private dirs and export them for Camel.init / MailSession."""
    data, cache = helper_camel_dirs(account_uid)
    os.makedirs(data, mode=0o700, exist_ok=True)
    os.makedirs(cache, mode=0o700, exist_ok=True)
    os.environ[ENV_DATA] = data
    os.environ[ENV_CACHE] = cache
    return data, cache


def resolve_camel_dirs() -> tuple[str, str]:
    """Dirs for this process: helper env overrides, else system Evolution."""
    data = (os.environ.get(ENV_DATA) or "").strip()
    cache = (os.environ.get(ENV_CACHE) or "").strip()
    if data and cache:
        return data, cache
    return default_evolution_dirs()
