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


def message_list_fingerprint(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(message.get("uid")) for message in messages)


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
