# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Map Post offline-body-sync preferences to Camel OfflineSettings."""

from __future__ import annotations

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.preferences import (
    OFFLINE_BODY_SYNC_ALL,
    OFFLINE_BODY_SYNC_LAST_MONTH,
    OFFLINE_BODY_SYNC_LAST_YEAR,
    OFFLINE_BODY_SYNC_OFF,
    OfflineBodySyncMode,
    get_account_offline_body_sync,
    get_account_user_online,
)

# Bare "(match-all)" fails CamelStoreSearch (EDS ≥ 3.58): argc must be 1.
# "(match-all #t)" matches everything on classic FolderSearch and StoreSearch (#277).
_DOWNSYNC_EXPRESSION = "(match-all #t)"


def downsync_expression_for_mode(mode: OfflineBodySyncMode) -> str | None:
    """Return Camel downsync S-expression, or None when sync is disabled."""
    if mode == OFFLINE_BODY_SYNC_OFF:
        return None
    return _DOWNSYNC_EXPRESSION


def apply_offline_settings_to_store(
    store: Camel.Store,
    account_uid: str,
    *,
    mode: OfflineBodySyncMode | None = None,
) -> OfflineBodySyncMode:
    """Configure Camel OfflineSettings from Post preference. Returns effective mode."""
    if mode is None:
        mode = get_account_offline_body_sync(account_uid)

    settings = store.ref_settings()
    if settings is None:
        return mode

    set_interval = getattr(settings, "set_store_changes_interval", None)
    if callable(set_interval):
        set_interval(0)

    if not isinstance(settings, Camel.OfflineSettings):
        return mode

    if mode == OFFLINE_BODY_SYNC_OFF:
        settings.set_stay_synchronized(False)
        return mode

    settings.set_stay_synchronized(True)
    if mode == OFFLINE_BODY_SYNC_ALL:
        settings.set_limit_by_age(False)
    elif mode == OFFLINE_BODY_SYNC_LAST_MONTH:
        settings.set_limit_by_age(True)
        settings.set_limit_unit(Camel.TimeUnit.MONTHS)
        settings.set_limit_value(1)
    elif mode == OFFLINE_BODY_SYNC_LAST_YEAR:
        settings.set_limit_by_age(True)
        settings.set_limit_unit(Camel.TimeUnit.YEARS)
        settings.set_limit_value(1)

    return mode


def apply_offline_sync_to_folder(
    folder: Camel.Folder,
    mode: OfflineBodySyncMode,
) -> None:
    """Enable or disable per-folder offline sync on Camel OfflineFolder instances."""
    if not isinstance(folder, Camel.OfflineFolder):
        return
    if mode == OFFLINE_BODY_SYNC_OFF:
        folder.set_offline_sync(Camel.ThreeState.OFF)
    else:
        folder.set_offline_sync(Camel.ThreeState.ON)


def account_is_user_offline(account_uid: str) -> bool:
    """Return True when the user has taken this account offline."""
    return not get_account_user_online(account_uid)
