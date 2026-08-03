# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""User-facing offline / cache status strings for the mail UI."""

from __future__ import annotations

OFFLINE_MAIL_MESSAGE = (
    "You're offline. Messages will load when you reconnect."
)
OFFLINE_CACHED_LIST_STATUS = "Offline · showing cached list"
OFFLINE_SEARCHING_LOCAL_CACHE = "Offline · searching local cache"
OFFLINE_CACHE_STATUS_PREFIX = "Caching mail for offline use"


def offline_cache_status_text(*, account_label: str, folder_name: str) -> str:
    folder = folder_name or "folders"
    # Body downsync caches MIME for headers Camel already knows; it does not
    # by itself grow the message list toward the full server folder (#208).
    return (
        f"{OFFLINE_CACHE_STATUS_PREFIX} · {account_label} · {folder} "
        f"(bodies for local headers)"
    )
