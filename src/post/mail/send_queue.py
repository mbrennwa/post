# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist outbound messages when the network is unavailable."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .helpers import paginate_messages

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from .send_errors import _is_localhost_refused

OFFLINE_MAIL_MESSAGE = (
    "You're offline. Messages will load when you reconnect."
)
OFFLINE_FOLDER_MESSAGE = "Offline — folders unavailable until you reconnect."


@dataclass
class QueuedOutboundMessage:
    account_uid: str
    to: list[str]
    cc: list[str] | None
    bcc: list[str] | None
    subject: str
    body: str
    in_reply_to: str | None = None
    references: str | None = None
    queued_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedOutboundMessage:
        return cls(
            account_uid=str(data["account_uid"]),
            to=list(data.get("to") or []),
            cc=list(data["cc"]) if data.get("cc") is not None else None,
            bcc=list(data["bcc"]) if data.get("bcc") is not None else None,
            subject=str(data.get("subject") or ""),
            body=str(data.get("body") or ""),
            in_reply_to=data.get("in_reply_to"),
            references=data.get("references"),
            queued_at=float(data.get("queued_at") or 0.0),
        )


def outbox_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "post", "outbox")


def is_queueable_network_error(exc: BaseException) -> bool:
    """Return True when a send failure may succeed if retried later."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, GLib.Error):
        text = exc.message or str(exc)
        if _is_localhost_refused(text):
            return False
        lowered = text.lower()
        return any(
            token in lowered
            for token in (
                "could not connect",
                "connection refused",
                "network is unreachable",
                "no route to host",
                "name or service not known",
                "temporary failure",
                "timed out",
                "timeout",
                "i/o error",
                "connection reset",
                "broken pipe",
                "network error",
            )
        )
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, GLib.Error):
        return exc.message or str(exc)
    return str(exc)


def is_network_unavailable_error(exc: BaseException) -> bool:
    """Return True for DNS/network failures and Camel offline-service errors."""
    if is_queueable_network_error(exc):
        return True
    lowered = _error_text(exc).lower()
    return any(
        token in lowered
        for token in (
            "must be working online",
            "error resolving",
            "name resolution",
        )
    )


def log_mail_error(logger: logging.Logger, message: str, exc: BaseException) -> None:
    if is_network_unavailable_error(exc):
        logger.debug("%s: %s", message, exc)
    else:
        logger.exception(message)


def offline_status_text(*, queued_count: int) -> str:
    if queued_count == 1:
        return "Offline · 1 message queued"
    if queued_count > 1:
        return f"Offline · {queued_count} messages queued"
    return "Offline"


def enqueue_outbound_message(message: QueuedOutboundMessage) -> str:
    directory = outbox_dir()
    os.makedirs(directory, exist_ok=True)
    queue_id = f"{int(time.time() * 1_000_000)}-{uuid.uuid4().hex}"
    payload = message.to_dict()
    payload["queued_at"] = message.queued_at or time.time()
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


def count_queued_for_account(account_uid: str) -> int:
    return sum(
        1
        for _, message in list_queued_outbound_messages()
        if message.account_uid == account_uid
    )


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
    return {
        "uid": queue_id,
        "subject": message.subject or "(No subject)",
        "from": from_label,
        "to": to_text,
        "preview_to": _preview_to_text(to_text),
        "sort_date": message.queued_at,
        "flags": {"seen": True},
    }


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
    queued_at = datetime.fromtimestamp(message.queued_at, tz=timezone.utc)
    date_str = queued_at.strftime("%Y-%m-%d %H:%M")
    return {
        "uid": queue_id,
        "subject": message.subject or "(No subject)",
        "from": from_label,
        "to": _format_address_field(message.to),
        "cc": _format_address_field(message.cc),
        "bcc": _format_address_field(message.bcc),
        "date_sent": date_str,
        "body_plain": message.body or "",
        "body_html": None,
        "flags": {"seen": True, "queued": True},
    }


def list_queued_messages_page(
    account_uid: str,
    *,
    from_label: str,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    items = list_queued_for_account(account_uid)
    total = len(items)
    messages = [
        queued_to_list_dict(queue_id, message, from_label=from_label)
        for queue_id, message in items
    ]
    page, has_more = paginate_messages(messages, offset, limit)
    return page, 0, total, has_more
