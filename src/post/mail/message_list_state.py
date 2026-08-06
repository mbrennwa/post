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


def is_archive_folder_name(folder_name: str | None) -> bool:
    """True for Archive / All Mail (often tens of thousands of messages)."""
    if not folder_name:
        return False
    lower = folder_name.strip().lower().replace("\\", "/")
    leaf = lower.rsplit("/", 1)[-1]
    if leaf in _ARCHIVE_LEAVES:
        return True
    return "[google mail]/all mail" in lower or "[gmail]/all mail" in lower


def is_trash_or_junk_folder_name(folder_name: str | None) -> bool:
    """True for Trash / Junk / Spam (often legitimately under 1000 messages)."""
    if not folder_name:
        return False
    leaf = _folder_leaf_name(folder_name)
    return leaf in _TRASH_LEAVES or leaf in _JUNK_LEAVES


def is_heavy_folder_name(folder_name: str | None) -> bool:
    """Return True for folders where a full Camel reindex often OOMs (#189/#208).

    Includes Archive / All Mail, Trash, and Junk — valuable mail can land in
    Trash/Junk by accident and must still be indexed for list/search.
    """
    return is_archive_folder_name(folder_name) or is_trash_or_junk_folder_name(
        folder_name
    )


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


def _normalize_rfc_message_id(value: Any) -> str | None:
    """Return a normalized RFC Message-ID, or None when missing/unusable."""
    mid = str(value or "").strip().strip("<>").lower()
    if not mid or mid == "0":
        return None
    # Require "@" so Camel summary hashes / bare tokens are not treated as RFC ids.
    if "@" not in mid:
        return None
    return mid


def _folder_message_id_hash(message: dict[str, Any]) -> int | None:
    """Return a non-zero Camel summary hash from index row fields."""
    raw = message.get("message_id_hash")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw != 0:
        return raw
    if isinstance(raw, str) and raw.isdigit() and raw != "0":
        return int(raw)
    # Legacy rows stored Camel's uint hash in ``message_id`` as a decimal string.
    mid = str(message.get("message_id") or "").strip().strip("<>").lower()
    if mid.isdigit() and mid != "0" and "@" not in mid:
        return int(mid)
    return None


def _folder_message_identity_key(message: dict[str, Any]) -> str:
    """Identity for collapsing grow-only duplicates of the same logical mail.

    Prefer RFC ``Message-ID``. Fall back to Camel's non-zero summary hash (stable
    across Graph RestId remaps). Do not merge on subject/from/date — too lossy
    for newsletters — and never treat hash ``0`` as a merge key (#265/#267).
    """
    rfc_mid = _normalize_rfc_message_id(message.get("message_id"))
    if rfc_mid:
        return f"mid:{rfc_mid}"
    mid_hash = _folder_message_id_hash(message)
    if mid_hash is not None:
        return f"hash:{mid_hash}"
    # No usable identity metadata — keep each UID distinct.
    return f"uid:{message.get('uid') or id(message)}"


def upsert_folder_index_by_identity(
    by_uid: dict[str, dict[str, Any]],
    message: dict[str, Any],
    *,
    prefer_uids: set[str] | None = None,
    by_identity: dict[str, str] | None = None,
) -> str | None:
    """Insert ``message`` into ``by_uid``, replacing a same-identity stale UID.

    Pass ``by_identity`` (identity key → uid) for O(1) lookups. Without it,
    this scans ``by_uid`` and is O(n) per call — too slow for large Archives.

    Returns the replaced (removed) UID when RestId A→B remap occurs, else None.
    """
    uid = str(message.get("uid") or "")
    if not uid:
        return None
    prefer = prefer_uids or set()
    incoming = dict(message)
    incoming["uid"] = uid
    key = _folder_message_identity_key(incoming)

    if uid in by_uid and by_identity is not None:
        prev_key = _folder_message_identity_key(by_uid[uid])
        if prev_key != key and by_identity.get(prev_key) == uid:
            by_identity.pop(prev_key, None)

    if key.startswith("uid:"):
        by_uid[uid] = incoming
        if by_identity is not None:
            by_identity[key] = uid
        return None

    old_uid: str | None = None
    if by_identity is not None:
        mapped = by_identity.get(key)
        if mapped and mapped != uid and mapped in by_uid:
            old_uid = mapped
        elif mapped and mapped != uid and mapped not in by_uid:
            by_identity.pop(key, None)
    else:
        for existing_uid, existing in by_uid.items():
            if existing_uid == uid:
                continue
            if _folder_message_identity_key(existing) == key:
                old_uid = existing_uid
                break

    if old_uid is None:
        by_uid[uid] = incoming
        if by_identity is not None:
            by_identity[key] = uid
        return None

    old_preferred = old_uid in prefer
    new_preferred = uid in prefer
    if new_preferred or not old_preferred:
        by_uid.pop(old_uid, None)
        by_uid[uid] = incoming
        if by_identity is not None:
            by_identity[key] = uid
        return old_uid

    # Keep the Camel-present UID; refresh metadata from the incoming row.
    kept = dict(by_uid[old_uid])
    for field in (
        "subject",
        "from",
        "to",
        "cc",
        "message_id",
        "message_id_hash",
        "sort_date",
        "date_sent",
        "date_received",
        "size",
        "flags",
    ):
        if field in incoming and incoming[field] is not None:
            kept[field] = incoming[field]
    by_uid[old_uid] = kept
    if by_identity is not None:
        by_identity[key] = old_uid
    return None


def normalize_folder_index_by_uid(
    messages: list[dict[str, Any]],
    *,
    prefer_uids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a UID map with one row per logical identity (seed normalize)."""
    by_uid: dict[str, dict[str, Any]] = {}
    by_identity: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        upsert_folder_index_by_identity(
            by_uid,
            message,
            prefer_uids=prefer_uids,
            by_identity=by_identity,
        )
    return by_uid


def prune_stale_folder_index_uids(
    by_uid: dict[str, dict[str, Any]],
    camel_uids: set[str],
    *,
    by_identity: dict[str, str] | None = None,
) -> list[str]:
    """Drop UIDs absent from Camel when the same identity has a live alternate.

    Safe only when callers already know Camel's UID set is complete enough that
    missing UIDs are RestId remaps, not not-yet-fetched mail (#267).
    """
    if not by_uid or not camel_uids:
        return []
    grouped: dict[str, list[str]] = {}
    for uid, message in by_uid.items():
        key = _folder_message_identity_key(message)
        grouped.setdefault(key, []).append(uid)

    removed: list[str] = []
    for key, uids in grouped.items():
        if key.startswith("uid:") or len(uids) < 2:
            continue
        live = [uid for uid in uids if uid in camel_uids]
        if not live:
            continue
        for uid in uids:
            if uid in camel_uids:
                continue
            by_uid.pop(uid, None)
            removed.append(uid)
        if by_identity is not None:
            by_identity[key] = live[0]
    return removed


def dedupe_folder_index_messages(
    messages: list[dict[str, Any]],
    *,
    prefer_uids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Collapse same-identity rows (legacy caches / tests).

    Heavy-folder indexing upserts by identity at the source (#267); this helper
    remains for one-shot normalize of older on-disk indexes.
    """
    if len(messages) < 2:
        return list(messages)
    by_uid = normalize_folder_index_by_uid(messages, prefer_uids=prefer_uids)
    if len(by_uid) == len(messages):
        return list(messages)

    prefer = prefer_uids or set()
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for message in messages:
        key = _folder_message_identity_key(message)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(message)

    deduped: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            chosen_uid = str(group[0].get("uid") or "")
            deduped.append(by_uid.get(chosen_uid, group[0]))
            continue
        preferred = [
            msg
            for msg in group
            if str(msg.get("uid") or "") in prefer
        ]
        if preferred:
            chosen = preferred[-1]
        else:
            chosen = group[-1]
        chosen_uid = str(chosen.get("uid") or "")
        deduped.append(by_uid.get(chosen_uid, chosen))
    return deduped


def folder_index_identity_keys(messages: list[dict[str, Any]]) -> set[str]:
    return {_folder_message_identity_key(msg) for msg in messages}


def folder_index_covers_identities(
    incoming: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> bool:
    """True when ``incoming`` covers every logical message in ``existing`` (#267)."""
    if not existing:
        return True
    return folder_index_identity_keys(incoming).issuperset(
        folder_index_identity_keys(existing)
    )


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
