# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist mail mutations when the network is unavailable."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

log = logging.getLogger(__name__)

OperationType = Literal[
    "move_to_trash",
    "archive",
    "move_to_folder",
    "set_seen",
    "set_flagged",
]


@dataclass
class QueuedOperation:
    op_type: OperationType
    account_uid: str
    folder_name: str
    message_uids: list[str]
    destination_folder: str | None = None
    seen: bool | None = None
    flagged: bool | None = None
    queued_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedOperation:
        return cls(
            op_type=str(data["op_type"]),  # type: ignore[arg-type]
            account_uid=str(data["account_uid"]),
            folder_name=str(data["folder_name"]),
            message_uids=[str(uid) for uid in data.get("message_uids") or []],
            destination_folder=(
                str(data["destination_folder"])
                if data.get("destination_folder") is not None
                else None
            ),
            seen=data.get("seen") if "seen" in data else None,
            flagged=data.get("flagged") if "flagged" in data else None,
            queued_at=float(data.get("queued_at") or 0.0),
        )


def operations_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "post", "operations")


def new_operation_queue_id() -> str:
    return f"{int(time.time() * 1_000_000)}-{uuid.uuid4().hex}"


def enqueue_operation(operation: QueuedOperation, *, queue_id: str | None = None) -> str:
    directory = operations_dir()
    os.makedirs(directory, exist_ok=True)
    queue_id = queue_id or new_operation_queue_id()
    payload = operation.to_dict()
    payload["queued_at"] = operation.queued_at or time.time()
    path = os.path.join(directory, f"{queue_id}.json")
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return queue_id


def _operations_match_for_coalesce(
    existing: QueuedOperation, operation: QueuedOperation
) -> bool:
    return (
        existing.account_uid == operation.account_uid
        and existing.op_type == operation.op_type
        and existing.folder_name == operation.folder_name
        and existing.destination_folder == operation.destination_folder
        and existing.seen == operation.seen
        and existing.flagged == operation.flagged
    )


def flush_lease_path(queue_id: str) -> str:
    return os.path.join(operations_dir(), f"{queue_id}.flushing")


def acquire_flush_lease(queue_id: str) -> None:
    """Mark ``queue_id`` in-flight so the UI process does not coalesce into it (#503)."""
    directory = operations_dir()
    os.makedirs(directory, exist_ok=True)
    path = flush_lease_path(queue_id)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{time.time()}\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def release_flush_lease(queue_id: str) -> None:
    try:
        os.unlink(flush_lease_path(queue_id))
    except FileNotFoundError:
        pass


def list_flush_leased_ids() -> set[str]:
    directory = operations_dir()
    if not os.path.isdir(directory):
        return set()
    leased: set[str] = set()
    try:
        names = os.listdir(directory)
    except OSError:
        return set()
    for name in names:
        if name.endswith(".flushing"):
            leased.add(name.removesuffix(".flushing"))
    return leased


def release_all_flush_leases() -> None:
    """Drop every flush lease (helper crash / respawn, #503 Phase 4)."""
    for queue_id in list(list_flush_leased_ids()):
        release_flush_lease(queue_id)


def coalesce_or_enqueue_operation(
    operation: QueuedOperation,
    *,
    skip_ids: set[str] | frozenset[str] | None = None,
) -> str:
    """Merge into a pending same-key op, or enqueue a new one (#462).

    ``skip_ids`` are operations currently executing (do not merge into them).
    Disk flush leases from the helper process are always skipped (#503).
    """
    skip = set(skip_ids or ())
    skip.update(list_flush_leased_ids())
    for queue_id, existing in list_queued_operations():
        if queue_id in skip:
            continue
        if not _operations_match_for_coalesce(existing, operation):
            continue
        merged_uids: list[str] = []
        seen_uids: set[str] = set()
        for uid in list(existing.message_uids) + list(operation.message_uids):
            text = str(uid)
            if not text or text in seen_uids:
                continue
            seen_uids.add(text)
            merged_uids.append(text)
        existing.message_uids = merged_uids
        # Rewrite in place so flush order / queue_id stay stable.
        enqueue_operation(existing, queue_id=queue_id)
        return queue_id
    return enqueue_operation(operation)


def list_queued_operations() -> list[tuple[str, QueuedOperation]]:
    directory = operations_dir()
    if not os.path.isdir(directory):
        return []

    queued: list[tuple[str, QueuedOperation]] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                continue
            queue_id = name.removesuffix(".json")
            queued.append((queue_id, QueuedOperation.from_dict(data)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    queued.sort(key=lambda item: item[1].queued_at)
    return queued


def remove_queued_operation(queue_id: str) -> None:
    path = os.path.join(operations_dir(), f"{queue_id}.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    release_flush_lease(queue_id)


def remove_uids_from_queued_operations(
    account_uid: str,
    folder_name: str,
    message_uids: list[str],
    *,
    op_types: frozenset[str] | set[str] | None = None,
    skip_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, list[str]]:
    """Strip UIDs from matching transfer ops for undo (#499).

    Ops under ``skip_ids`` or a disk flush lease are left alone (in flight).
    Empty ops are deleted. Returns ``cancelled_uids`` and ``in_flight_uids``.
    """
    wanted = {str(uid) for uid in message_uids if uid}
    if not wanted:
        return {"cancelled_uids": [], "in_flight_uids": []}

    transfer_types = op_types or frozenset(
        {"archive", "move_to_trash", "move_to_folder"}
    )
    skip = set(skip_ids or ())
    skip.update(list_flush_leased_ids())

    cancelled: list[str] = []
    cancelled_seen: set[str] = set()
    in_flight: list[str] = []
    in_flight_seen: set[str] = set()

    for queue_id, operation in list_queued_operations():
        if operation.account_uid != account_uid:
            continue
        if operation.folder_name != folder_name:
            continue
        if operation.op_type not in transfer_types:
            continue
        overlap = [uid for uid in operation.message_uids if uid in wanted]
        if not overlap:
            continue
        if queue_id in skip:
            for uid in overlap:
                if uid not in in_flight_seen:
                    in_flight_seen.add(uid)
                    in_flight.append(uid)
            continue
        remaining = [uid for uid in operation.message_uids if uid not in wanted]
        for uid in overlap:
            if uid not in cancelled_seen:
                cancelled_seen.add(uid)
                cancelled.append(uid)
        if remaining:
            operation.message_uids = remaining
            enqueue_operation(operation, queue_id=queue_id)
        else:
            remove_queued_operation(queue_id)

    # UIDs never found in the queue are treated as already gone / flushed.
    return {"cancelled_uids": cancelled, "in_flight_uids": in_flight}


def count_queued_operations() -> int:
    return len(list_queued_operations())


def operation_action_label(op_type: str) -> str:
    """Short user-facing verb for a queued mutation (#491)."""
    return {
        "archive": "Archive",
        "move_to_trash": "Trash",
        "move_to_folder": "Move",
        "set_seen": "Mark read",
        "set_flagged": "Flag",
    }.get(op_type, "Sync")


def queued_operations_for_account(
    account_uid: str | None = None,
) -> list[QueuedOperation]:
    ops = [operation for _queue_id, operation in list_queued_operations()]
    if account_uid is None:
        return ops
    return [operation for operation in ops if operation.account_uid == account_uid]


def format_queued_operation_status(
    *,
    account_label: str,
    op_type: str,
    folder_name: str,
    message_count: int,
    blocker: str,
) -> str:
    """Status bar line for a durable mutation queue item (#491 / #503)."""
    action = operation_action_label(op_type)
    folder = folder_name.strip() or "folder"
    account = account_label.strip() or "account"
    if message_count <= 1:
        what = f"{action} in {account}/{folder}"
    else:
        what = f"{action} {message_count} in {account}/{folder}"
    return f"Queued: {what} — {blocker}"


def offline_queue_status_text(
    *,
    send_queued_count: int,
    operation_queued_count: int,
    draft_queued_count: int = 0,
) -> str:
    parts: list[str] = []
    if send_queued_count == 1:
        parts.append("1 message queued")
    elif send_queued_count > 1:
        parts.append(f"{send_queued_count} messages queued")
    if draft_queued_count == 1:
        parts.append("1 draft queued")
    elif draft_queued_count > 1:
        parts.append(f"{draft_queued_count} drafts queued")
    if operation_queued_count == 1:
        parts.append("1 action queued")
    elif operation_queued_count > 1:
        parts.append(f"{operation_queued_count} actions queued")
    if not parts:
        return "Offline"
    return "Offline · " + " · ".join(parts)
