# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Map Camel's empty ``evolution/mail/<uid>/`` cache path to the real store."""

from __future__ import annotations

import hashlib
import os

_MAIL_INFIX = f"{os.sep}.cache{os.sep}evolution{os.sep}mail{os.sep}"
_STORE_INFIX = f"{os.sep}.cache{os.sep}evolution{os.sep}"


def alternate_evolution_cache_path(filename: str) -> str | None:
    """Return the non-``mail/`` twin of a Camel ``get_filename`` path, if any."""
    if _MAIL_INFIX not in filename:
        return None
    alt = filename.replace(_MAIL_INFIX, _STORE_INFIX, 1)
    return alt if alt != filename else None


def cached_rfc822_candidates(filename: str) -> tuple[str, ...]:
    """``get_filename`` result, then the rewritten evolution-root path."""
    alt = alternate_evolution_cache_path(filename)
    if alt is None:
        return (filename,)
    return (filename, alt)


def rfc822_digest(api_uid: str) -> str:
    return hashlib.md5(api_uid.encode("utf-8")).hexdigest()


def is_nonempty_rfc822(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def first_nonempty_path(paths: tuple[str, ...] | list[str]) -> str | None:
    for path in paths:
        if is_nonempty_rfc822(path):
            return path
    return None


def find_nonempty_rfc822(
    store_root: str,
    folder_names: tuple[str, ...] | list[str],
    digest: str,
) -> str | None:
    """Look up MD5(uid) under ``store_root/folders/<name>/cur/<subdir>/``.

    Lists only that ``cur/`` directory (typically ≤100 names). Does not walk
    the rest of the Evolution cache.
    """
    seen: set[str] = set()
    for folder_name in folder_names:
        if not folder_name or folder_name in seen:
            continue
        seen.add(folder_name)
        cur = os.path.join(store_root, "folders", folder_name, "cur")
        try:
            names = os.listdir(cur)
        except OSError:
            continue
        for name in names:
            path = os.path.join(cur, name, digest)
            if is_nonempty_rfc822(path):
                return path
    return None


def evolution_store_roots(cache_root: str, account_uid: str) -> tuple[str, str]:
    return (
        os.path.join(cache_root, account_uid),
        os.path.join(cache_root, "mail", account_uid),
    )
