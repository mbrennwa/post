# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Camel message flag operations without loading EDS."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.camel_util import (
    camel_uid_to_api,
    folder_get_message_info,
    folder_get_unread_count,
)

# Outlook Flag is Follow Up on these Camel backends; FLAGGED maps to Importance.
FOLLOW_UP_FLAG_BACKENDS = frozenset({"microsoft365", "ews"})

_FOLLOW_UP_TAG = "follow-up"
_COMPLETED_ON_TAG = "completed-on"
_DUE_BY_TAG = "due-by"
_FOLLOW_UP_START_TAG = "follow-up-start"


def uses_follow_up_flag(backend: str | None) -> bool:
    """Return True when Flag UI should use Follow Up user tags, not FLAGGED."""
    return (backend or "").lower() in FOLLOW_UP_FLAG_BACKENDS


def _user_tag_value(info: Any, name: str) -> str | None:
    value = info.get_user_tag(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def message_info_is_flagged(info: Any, *, backend: str | None = None) -> bool:
    """Return whether *info* should show as flagged for *backend*."""
    if uses_follow_up_flag(backend):
        follow_up = _user_tag_value(info, _FOLLOW_UP_TAG)
        if not follow_up:
            return False
        return _user_tag_value(info, _COMPLETED_ON_TAG) is None
    return bool(info.get_flags() & Camel.MessageFlags.FLAGGED)


def apply_message_flagged(
    folder: Camel.Folder,
    message_uid: str,
    flagged: bool,
    *,
    backend: str | None = None,
    on_flagged_changed: Callable[[bool], None] | None = None,
) -> bool:
    """Set or clear the user-facing Flag for *message_uid*.

    On microsoft365/ews this writes Follow Up user tags (Outlook Flag).
    Elsewhere it toggles ``CAMEL_MESSAGE_FLAGGED`` (IMAP ``\\Flagged``).
    """
    if not uses_follow_up_flag(backend):
        flag_value = Camel.MessageFlags.FLAGGED if flagged else 0
        return apply_message_flags(
            folder,
            message_uid,
            Camel.MessageFlags.FLAGGED,
            flag_value,
            on_flagged_changed=on_flagged_changed,
        )

    info = folder_get_message_info(folder, message_uid)
    if info is None:
        return False

    currently = message_info_is_flagged(info, backend=backend)
    if currently == flagged:
        return False

    if flagged:
        changed = bool(info.set_user_tag(_FOLLOW_UP_TAG, _FOLLOW_UP_TAG))
        # Clearing completed-on keeps status as active Follow Up, not Complete.
        if info.get_user_tag(_COMPLETED_ON_TAG) is not None:
            changed = bool(info.set_user_tag(_COMPLETED_ON_TAG, None)) or changed
    else:
        changed = False
        for tag in (
            _FOLLOW_UP_TAG,
            _COMPLETED_ON_TAG,
            _DUE_BY_TAG,
            _FOLLOW_UP_START_TAG,
        ):
            changed = bool(info.set_user_tag(tag, None)) or changed

    if not changed:
        return False

    info.set_folder_flagged(True)
    if on_flagged_changed is not None:
        on_flagged_changed(flagged)
    return True


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
