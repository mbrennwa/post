# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist outbound messages when the network is unavailable."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .compose import ComposeAttachment
from .helpers import format_message_datetime, paginate_messages


@dataclass
class QueuedOutboundMessage:
    account_uid: str
    to: list[str]
    cc: list[str] | None
    bcc: list[str] | None
    subject: str
    body: str
    body_html: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    queued_at: float = 0.0
    send_after: float | None = None
    attachments: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedOutboundMessage:
        raw_attachments = data.get("attachments")
        attachments: list[dict[str, str]] | None
        if raw_attachments is None:
            attachments = None
        else:
            attachments = [
                {
                    "filename": str(item.get("filename") or "attachment"),
                    "mime_type": str(
                        item.get("mime_type") or "application/octet-stream"
                    ),
                    "path": str(item.get("path") or ""),
                }
                for item in raw_attachments
                if isinstance(item, dict)
            ]
        return cls(
            account_uid=str(data["account_uid"]),
            to=list(data.get("to") or []),
            cc=list(data["cc"]) if data.get("cc") is not None else None,
            bcc=list(data["bcc"]) if data.get("bcc") is not None else None,
            subject=str(data.get("subject") or ""),
            body=str(data.get("body") or ""),
            body_html=(
                str(data["body_html"]) if data.get("body_html") is not None else None
            ),
            in_reply_to=data.get("in_reply_to"),
            references=data.get("references"),
            queued_at=float(data.get("queued_at") or 0.0),
            send_after=(
                float(data["send_after"])
                if data.get("send_after") is not None
                else None
            ),
            attachments=attachments,
        )


def is_outbound_ready_to_send(
    message: QueuedOutboundMessage,
    *,
    now: float | None = None,
) -> bool:
    """Return True when a queued message may be delivered now."""
    if now is None:
        now = time.time()
    if message.send_after is None:
        return True
    return message.send_after <= now


def has_pending_send_delay(
    message: QueuedOutboundMessage,
    *,
    now: float | None = None,
) -> bool:
    """Return True when a queued message is still waiting on send delay."""
    if now is None:
        now = time.time()
    return message.send_after is not None and message.send_after > now


def format_stop_sending_toast(
    moved_by_account_label: Mapping[str, int] | Sequence[tuple[str, int]],
) -> str:
    """Toast after stopping delayed sends and moving messages to Drafts."""
    items = _merge_account_counts(moved_by_account_label)
    if not items:
        return ""
    total = sum(count for _label, count in items)
    if total == 1:
        return f"Moved message to Drafts: {items[0][0]}"
    if len(items) == 1:
        return f"Moved {total} messages to Drafts: {items[0][0]}"
    parts = [f"{label} ({count})" for label, count in items]
    return "Moved messages to Drafts: " + ", ".join(parts)


def format_stop_sending_error_toast(
    failed_by_account_label: Mapping[str, int] | Sequence[tuple[str, int]],
) -> str:
    """Toast when some delayed messages could not be moved to Drafts."""
    items = _merge_account_counts(failed_by_account_label)
    if not items:
        return "Could not move to Drafts"
    total = sum(count for _label, count in items)
    if total == 1:
        return f"Could not move message to Drafts: {items[0][0]}"
    parts = [f"{label} ({count})" for label, count in items]
    return "Could not move messages to Drafts: " + ", ".join(parts)


def _merge_account_counts(
    by_account_label: Mapping[str, int] | Sequence[tuple[str, int]],
) -> list[tuple[str, int]]:
    merged: dict[str, int] = {}
    if isinstance(by_account_label, Mapping):
        pairs = by_account_label.items()
    else:
        pairs = by_account_label
    for label, count in pairs:
        if count <= 0:
            continue
        key = str(label)
        merged[key] = merged.get(key, 0) + int(count)
    return sorted(merged.items(), key=lambda item: item[0].casefold())


def outbox_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "post", "outbox")


def _queued_attachment_dir(queue_id: str) -> str:
    return os.path.join(outbox_dir(), queue_id)


def _write_attachment_sidecars(
    queue_id: str,
    attachments: Sequence[ComposeAttachment],
) -> list[dict[str, str]]:
    if not attachments:
        return []
    directory = _queued_attachment_dir(queue_id)
    os.makedirs(directory, exist_ok=True)
    refs: list[dict[str, str]] = []
    for index, attachment in enumerate(attachments):
        rel_path = str(index)
        path = os.path.join(directory, rel_path)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(attachment.data)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        refs.append(
            {
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "path": rel_path,
            }
        )
    return refs


def load_queued_attachments(
    queue_id: str,
    message: QueuedOutboundMessage,
) -> list[ComposeAttachment]:
    if not message.attachments:
        return []
    directory = _queued_attachment_dir(queue_id)
    loaded: list[ComposeAttachment] = []
    for ref in message.attachments:
        rel_path = ref.get("path")
        if not rel_path:
            continue
        path = os.path.join(directory, rel_path)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        loaded.append(
            ComposeAttachment(
                filename=ref.get("filename") or "attachment",
                mime_type=ref.get("mime_type") or "application/octet-stream",
                data=data,
            )
        )
    return loaded


def load_queued_outbound_message(queue_id: str) -> QueuedOutboundMessage:
    path = os.path.join(outbox_dir(), f"{queue_id}.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid queued message: {queue_id}")
    return QueuedOutboundMessage.from_dict(data)


def try_load_queued_outbound_message(
    queue_id: str,
) -> QueuedOutboundMessage | None:
    """Load a queued message, or None if the outbox file is already gone."""
    try:
        return load_queued_outbound_message(queue_id)
    except FileNotFoundError:
        return None


def new_outbound_queue_id() -> str:
    return f"{int(time.time() * 1_000_000)}-{uuid.uuid4().hex}"


def persist_outbound_send(
    *,
    account_uid: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    subject: str,
    body: str,
    body_html: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[ComposeAttachment] | None = None,
    queue_id: str | None = None,
    send_after: float | None = None,
) -> str:
    """Write an outbound message to the outbox before attempting delivery."""
    return enqueue_outbound_message(
        QueuedOutboundMessage(
            account_uid=account_uid,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            send_after=send_after,
        ),
        attachment_payloads=attachments,
        queue_id=queue_id,
    )


def enqueue_outbound_message(
    message: QueuedOutboundMessage,
    *,
    attachment_payloads: Sequence[ComposeAttachment] | None = None,
    queue_id: str | None = None,
) -> str:
    directory = outbox_dir()
    os.makedirs(directory, exist_ok=True)
    queue_id = queue_id or new_outbound_queue_id()
    if attachment_payloads:
        message.attachments = _write_attachment_sidecars(queue_id, attachment_payloads)
    payload = message.to_dict()
    payload["queued_at"] = message.queued_at or time.time()
    if message.send_after is not None:
        payload["send_after"] = message.send_after
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


def _rewrite_queued_outbound_message(
    queue_id: str,
    message: QueuedOutboundMessage,
) -> None:
    directory = outbox_dir()
    os.makedirs(directory, exist_ok=True)
    payload = message.to_dict()
    payload["queued_at"] = message.queued_at or time.time()
    if message.send_after is not None:
        payload["send_after"] = message.send_after
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


def clear_outbound_send_delay(queue_id: str) -> bool:
    """Clear send_after so a queued message may be delivered immediately."""
    message = load_queued_outbound_message(queue_id)
    if not has_pending_send_delay(message):
        return False
    message.send_after = None
    _rewrite_queued_outbound_message(queue_id, message)
    return True


def list_queued_outbound_messages() -> list[tuple[str, QueuedOutboundMessage]]:
    directory = outbox_dir()
    if not os.path.isdir(directory):
        return []

    queued: list[tuple[str, QueuedOutboundMessage]] = []
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
            queued.append((queue_id, QueuedOutboundMessage.from_dict(data)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return queued


def remove_queued_outbound_message(queue_id: str) -> None:
    path = os.path.join(outbox_dir(), f"{queue_id}.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    attachment_dir = _queued_attachment_dir(queue_id)
    if os.path.isdir(attachment_dir):
        shutil.rmtree(attachment_dir, ignore_errors=True)


def count_queued_for_account(account_uid: str) -> int:
    return sum(
        1
        for _, message in list_queued_outbound_messages()
        if message.account_uid == account_uid
    )


def list_pending_delayed_outbound_messages() -> list[tuple[str, QueuedOutboundMessage]]:
    """Outbox items waiting on send delay (not offline-only queue entries)."""
    return [
        (queue_id, message)
        for queue_id, message in list_queued_outbound_messages()
        if message.send_after is not None
    ]


def list_queued_for_account(
    account_uid: str,
) -> list[tuple[str, QueuedOutboundMessage]]:
    queued = [
        (queue_id, message)
        for queue_id, message in list_queued_outbound_messages()
        if message.account_uid == account_uid
    ]
    queued.sort(key=lambda item: item[1].queued_at, reverse=True)
    return queued


def _format_address_field(addrs: list[str] | None) -> str:
    if not addrs:
        return ""
    return ", ".join(addrs)


def _preview_to_text(to_text: str) -> str:
    if len(to_text) <= 60:
        return to_text
    return to_text[:57] + "..."


def queued_to_list_dict(
    queue_id: str,
    message: QueuedOutboundMessage,
    *,
    from_label: str,
) -> dict[str, Any]:
    to_text = _format_address_field(message.to)
    item: dict[str, Any] = {
        "uid": queue_id,
        "subject": message.subject or "(No subject)",
        "from": from_label,
        "to": to_text,
        "preview_to": _preview_to_text(to_text),
        "sort_date": message.queued_at,
        "flags": {"seen": True},
    }
    if message.send_after is not None:
        item["send_after"] = message.send_after
    return item


def read_queued_message(
    queue_id: str,
    *,
    account_uid: str,
    from_label: str,
) -> dict[str, Any]:
    path = os.path.join(outbox_dir(), f"{queue_id}.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Invalid queued message")
    message = QueuedOutboundMessage.from_dict(data)
    if message.account_uid != account_uid:
        raise ValueError("Queued message belongs to another account")
    date_str = (format_message_datetime(message.queued_at) or "")[:16]
    return {
        "uid": queue_id,
        "subject": message.subject or "(No subject)",
        "from": from_label,
        "to": _format_address_field(message.to),
        "cc": _format_address_field(message.cc),
        "bcc": _format_address_field(message.bcc),
        "date_sent": date_str,
        "body_plain": message.body or "",
        "body_html": message.body_html,
        "flags": {"seen": True, "queued": True},
    }


def list_queued_messages(
    account_uid: str,
    *,
    from_label: str,
) -> tuple[list[dict[str, Any]], int, int]:
    items = list_queued_for_account(account_uid)
    total = len(items)
    messages = [
        queued_to_list_dict(queue_id, message, from_label=from_label)
        for queue_id, message in items
    ]
    return messages, 0, total


def list_queued_messages_page(
    account_uid: str,
    *,
    from_label: str,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    messages, unread, total = list_queued_messages(
        account_uid,
        from_label=from_label,
    )
    page, has_more = paginate_messages(messages, offset, limit)
    return page, unread, total, has_more
