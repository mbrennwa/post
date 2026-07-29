# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Camel message flag operations without loading EDS."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.camel_util import (
    camel_uid_to_api,
    folder_get_message_info,
    folder_get_unread_count,
)


def persist_folder_flags(
    store: Camel.Store,
    folder: Camel.Folder,
    message_uids: list[str],
) -> None:
    """Save folder summary and push flag changes to the mail store."""
    summary = folder.get_folder_summary()
    if summary is not None:
        summary.touch()
        if not summary.save():
            raise RuntimeError("Could not save folder summary after flag change")

    for message_uid in message_uids:
        if not folder.synchronize_message_sync(camel_uid_to_api(message_uid), None):
            raise RuntimeError(
                f"Could not synchronize message {message_uid} after flag change"
            )

    if not folder.synchronize_sync(False, None):
        raise RuntimeError("Could not synchronize folder after flag change")

    if (
        store.get_connection_status() == Camel.ServiceConnectionStatus.CONNECTED
        and not store.synchronize_sync(False, None)
    ):
        raise RuntimeError("Could not synchronize mail store after flag change")


def apply_message_flags(
    folder: Camel.Folder,
    message_uid: str,
    mask: int,
    value: int,
    *,
    on_seen_changed: Callable[[bool], None] | None = None,
    on_flagged_changed: Callable[[bool], None] | None = None,
) -> bool:
    info = folder_get_message_info(folder, message_uid)
    if info is None:
        return False

    current = info.get_flags() & mask
    target = value & mask
    if current == target:
        return False

    api_uid = camel_uid_to_api(message_uid)
    if not folder.set_message_flags(api_uid, mask, value):
        return False

    info = folder_get_message_info(folder, message_uid)
    if info is not None:
        info.set_folder_flagged(True)

    if mask & Camel.MessageFlags.SEEN and on_seen_changed is not None:
        on_seen_changed(bool(value & Camel.MessageFlags.SEEN))
    if mask & Camel.MessageFlags.FLAGGED and on_flagged_changed is not None:
        on_flagged_changed(bool(value & Camel.MessageFlags.FLAGGED))
    return True


def mark_message_seen(
    folder: Camel.Folder,
    message_uid: str,
    *,
    persist_uids: Callable[[list[str]], None],
    on_seen_changed: Callable[[bool], None] | None = None,
    update_folder_counts: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Mark a message seen without refreshing the whole folder summary."""
    changed = apply_message_flags(
        folder,
        message_uid,
        Camel.MessageFlags.SEEN,
        Camel.MessageFlags.SEEN,
        on_seen_changed=on_seen_changed,
    )
    unread = folder_get_unread_count(folder)
    total = folder.get_message_count()
    if update_folder_counts is not None:
        update_folder_counts(unread, total)
    if changed:
        persist_uids([message_uid])
    return unread, total
