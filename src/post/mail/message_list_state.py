# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure helpers for message list caching and fingerprint comparison."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any, TypeVar

_K = TypeVar("_K", bound=Hashable)
_V = TypeVar("_V")

DEFAULT_FOLDER_LIST_CACHE_SIZE = 2
MESSAGE_LIST_UI_BATCH_SIZE = 100
# Hard cap for rows bound into Gtk.ListView on one folder open. Binding tens of
# thousands of GObjects (e.g. M365 Archive) freezes the UI / OOMs on startup.
# Additional rows are appended when the user scrolls near the end (#208).
MESSAGE_LIST_UI_BIND_CAP = 500
# How many more rows to append each time the list approaches the end.
MESSAGE_LIST_UI_BIND_MORE = 500
# UID / header materialization batch size for heavy-folder background index (#208).
HEAVY_FOLDER_INDEX_BATCH_SIZE = 250

# Offline body backfill priority: lower number runs first (#208).
OFFLINE_PRIORITY_ORDINARY = 0
OFFLINE_PRIORITY_ARCHIVE = 1
OFFLINE_PRIORITY_TRASH = 2
OFFLINE_PRIORITY_JUNK = 3

_ARCHIVE_LEAVES = frozenset({"archive", "archives", "all mail", "allmail"})
_TRASH_LEAVES = frozenset({"trash", "deleted items", "deleted messages", "bin"})
_JUNK_LEAVES = frozenset({"junk", "spam", "junk e-mail", "junk email"})


def _folder_leaf_name(folder_name: str | None) -> str:
    if not folder_name:
        return ""
    lower = folder_name.strip().lower().replace("\\", "/")
    return lower.rsplit("/", 1)[-1]


def is_heavy_folder_name(folder_name: str | None) -> bool:
    """Return True for folders where a full Camel reindex often OOMs (#189/#208).

    Includes Archive / All Mail, Trash, and Junk — valuable mail can land in
    Trash/Junk by accident and must still be indexed for list/search.
    """
    if not folder_name:
        return False
    lower = folder_name.strip().lower().replace("\\", "/")
    leaf = lower.rsplit("/", 1)[-1]
    if leaf in _ARCHIVE_LEAVES or leaf in _TRASH_LEAVES or leaf in _JUNK_LEAVES:
        return True
    if "[google mail]/all mail" in lower or "[gmail]/all mail" in lower:
        return True
    return False


def offline_folder_priority(
    folder_name: str | None,
    *,
    folder_flags: int = 0,
    type_archive: int = 0,
    type_trash: int = 0,
    type_junk: int = 0,
) -> int:
    """Return offline backfill priority (lower runs first).

    Ordinary folders first, then Archive/All Mail, then Trash, then Junk.
    """
    if type_junk and folder_flags & type_junk:
        return OFFLINE_PRIORITY_JUNK
    if type_trash and folder_flags & type_trash:
        return OFFLINE_PRIORITY_TRASH
    if type_archive and folder_flags & type_archive:
        return OFFLINE_PRIORITY_ARCHIVE

    leaf = _folder_leaf_name(folder_name)
    lower = (folder_name or "").strip().lower().replace("\\", "/")
    if leaf in _JUNK_LEAVES:
        return OFFLINE_PRIORITY_JUNK
    if leaf in _TRASH_LEAVES:
        return OFFLINE_PRIORITY_TRASH
    if leaf in _ARCHIVE_LEAVES:
        return OFFLINE_PRIORITY_ARCHIVE
    if "[google mail]/all mail" in lower or "[gmail]/all mail" in lower:
        return OFFLINE_PRIORITY_ARCHIVE
    return OFFLINE_PRIORITY_ORDINARY


def message_batch_ranges(
    count: int,
    batch_size: int = MESSAGE_LIST_UI_BATCH_SIZE,
) -> list[tuple[int, int]]:
    """Return (start, end) slices covering range(count) in batch_size chunks."""
    if count <= 0 or batch_size <= 0:
        return []
    return [
        (start, min(start + batch_size, count))
        for start in range(0, count, batch_size)
    ]


def message_list_fingerprint(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        f"{message.get('uid')}:{message.get('subject') or ''}"
        for message in messages
    )


def message_lists_equivalent_for_ui(
    current: list[dict[str, Any]],
    refreshed: list[dict[str, Any]],
    *,
    current_total: int,
    refreshed_total: int,
    sample: int = 32,
) -> bool:
    """Cheap equality for deciding whether to rebind the message list.

    Full fingerprints of multi-thousand folders stall the GTK main thread.
    """
    if current_total != refreshed_total:
        return False
    if len(current) != len(refreshed):
        return False
    if not refreshed:
        return True
    if len(refreshed) <= sample * 2:
        return message_list_fingerprint(current) == message_list_fingerprint(
            refreshed
        )
    return message_list_fingerprint(
        current[:sample]
    ) == message_list_fingerprint(refreshed[:sample]) and message_list_fingerprint(
        current[-sample:]
    ) == message_list_fingerprint(refreshed[-sample:])


def prepended_message_count(
    current: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> int:
    """Return how many messages were added at the front, or 0 if not a pure prepend."""
    old = message_list_fingerprint(current)
    new = message_list_fingerprint(messages)
    if len(new) <= len(old):
        return 0
    extra = len(new) - len(old)
    if new[extra:] == old:
        return extra
    return 0


def folder_list_ready_to_cache(
    shown: int,
    total: int,
    populating: bool,
    messages: list[dict[str, Any]] | None,
) -> bool:
    if populating or messages is None:
        return False
    if not messages:
        return total == 0
    return shown == len(messages) and (total < 0 or shown == total)


def folder_cache_matches(
    messages: list[dict[str, Any]],
    total: int,
    fingerprint: tuple[str, ...],
) -> bool:
    if message_list_fingerprint(messages) != fingerprint:
        return False
    return total < 0 or total == len(messages)


@dataclass
class FolderListSnapshot:
    """Detached Gtk rows and metadata for a fully-built folder list."""

    rows: list[Any]
    fingerprint: tuple[str, ...]
    messages: list[dict[str, Any]]
    shown_count: int
    total: int
    source: str
    scroll_value: float
    selected_uid: str | None


def touch_lru_cache(
    cache: OrderedDict[_K, _V],
    key: _K,
    value: _V,
    *,
    max_size: int = DEFAULT_FOLDER_LIST_CACHE_SIZE,
) -> None:
    if key in cache:
        del cache[key]
    cache[key] = value
    while len(cache) > max_size:
        cache.popitem(last=False)


def invalidate_lru_cache(cache: OrderedDict[_K, _V], key: _K) -> None:
    cache.pop(key, None)
