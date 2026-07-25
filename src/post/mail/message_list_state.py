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
MESSAGE_LIST_UI_BIND_CAP = 500


def is_heavy_folder_name(folder_name: str | None) -> bool:
    """Return True for folders where a full Camel reindex often OOMs (#189)."""
    if not folder_name:
        return False
    lower = folder_name.strip().lower().replace("\\", "/")
    leaf = lower.rsplit("/", 1)[-1]
    if leaf in {"archive", "archives", "all mail", "allmail"}:
        return True
    if "[google mail]/all mail" in lower or "[gmail]/all mail" in lower:
        return True
    return False


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
