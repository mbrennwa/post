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


def count_queued_operations() -> int:
    return len(list_queued_operations())


def offline_queue_status_text(
    *,
    send_queued_count: int,
    operation_queued_count: int,
) -> str:
    parts: list[str] = []
    if send_queued_count == 1:
        parts.append("1 message queued")
    elif send_queued_count > 1:
        parts.append(f"{send_queued_count} messages queued")
    if operation_queued_count == 1:
        parts.append("1 action queued")
    elif operation_queued_count > 1:
        parts.append(f"{operation_queued_count} actions queued")
    if not parts:
        return "Offline"
    return "Offline · " + " · ".join(parts)
