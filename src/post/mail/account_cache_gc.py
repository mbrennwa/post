# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Drop Post mail caches whose EDS Mail Account source is gone (#366)."""

from __future__ import annotations

import logging

from . import correspondent_cache
from . import folder_index_cache
from . import folder_status_cache

log = logging.getLogger(__name__)


def drop_orphan_account_caches(live_uids: set[str]) -> list[str]:
    """Remove folder-index / folder-status / correspondents dirs not in ``live_uids``.

    Does not migrate or rename. An empty ``live_uids`` drops every cached uid;
    callers must skip this helper when the EDS listing failed.
    """
    cached = (
        set(folder_index_cache.cached_account_uids())
        | set(folder_status_cache.cached_account_uids())
        | set(correspondent_cache.cached_account_uids())
    )
    dropped: list[str] = []
    for account_uid in sorted(cached):
        if account_uid in live_uids:
            continue
        log.info("Dropping leftover mail caches for removed account %s", account_uid)
        folder_index_cache.invalidate_account(account_uid)
        folder_status_cache.invalidate_account(account_uid)
        correspondent_cache.invalidate_account(account_uid)
        dropped.append(account_uid)
    return dropped
