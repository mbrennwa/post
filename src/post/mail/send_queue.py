# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist outbound messages when the network is unavailable."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .compose import ComposeAttachment
from .helpers import format_message_datetime, paginate_messages

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from .send_errors import _is_localhost_refused

OFFLINE_MAIL_MESSAGE = (
    "You're offline. Messages will load when you reconnect."
)
OFFLINE_FOLDER_MESSAGE = "Offline — folders unavailable until you reconnect."
SIGN_IN_FOLDER_MESSAGE = (
    "Sign-in required — reconnect this account to load folders."
)
TOKEN_EXPIRED_FOLDER_MESSAGE = (
    "Sign-in expired — open Settings → Online Accounts to reconnect."
)
OFFLINE_CACHED_LIST_STATUS = "Offline · showing cached list"
OFFLINE_SEARCHING_LOCAL_CACHE = "Offline · searching local cache"
OFFLINE_CACHE_STATUS_PREFIX = "Caching mail for offline use"


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


def is_sign_in_required_error(exc: BaseException) -> bool:
    """Return True when the account needs interactive re-authentication."""
    lowered = _error_text(exc).lower()
    return any(
        token in lowered
        for token in (
            "access token",
            "refresh token",
            "aadsts",
            "goa-error",
            "authentication",
            "auth failed",
            "invalid credentials",
            "login failed",
            "password",
            "sign-in",
            "sign in",
            "oauth",
        )
    )


def format_folder_load_error(exc: BaseException) -> str:
    """User-facing folder-list failure text (never raw GLib/Camel dumps)."""
    if is_network_unavailable_error(exc):
        return OFFLINE_FOLDER_MESSAGE
    lowered = _error_text(exc).lower()
    if any(
        token in lowered
        for token in ("access token", "refresh token", "aadsts", "goa-error", "oauth")
    ):
        return TOKEN_EXPIRED_FOLDER_MESSAGE
    if is_sign_in_required_error(exc):
        return SIGN_IN_FOLDER_MESSAGE
    return "Could not load folders for this account."


def format_sign_in_required_log(exc: BaseException) -> str:
    """Short log detail for expected auth failures (no AADSTS / GOA dumps)."""
    text = _error_text(exc)
    lowered = text.lower()
    if any(
        token in lowered
        for token in ("access token", "refresh token", "aadsts", "goa-error", "oauth")
    ):
        return "sign-in expired or invalid (re-auth required)"
    if "password" in lowered or "credentials" in lowered:
        return "password or credentials required"
    return "sign-in required"


def log_mail_error(logger: logging.Logger, message: str, exc: BaseException) -> None:
    if is_network_unavailable_error(exc):
        logger.debug("%s: %s", message, exc)
    elif is_sign_in_required_error(exc):
        # Expected until the user re-auths (expired GOA/OAuth token, etc.).
        logger.warning("%s: %s", message, format_sign_in_required_log(exc))
    else:
        logger.exception(message)


def offline_status_text(*, queued_count: int) -> str:
    if queued_count == 1:
        return "Offline · 1 message queued"
    if queued_count > 1:
        return f"Offline · {queued_count} messages queued"
    return "Offline"


def offline_cache_status_text(*, account_label: str, folder_name: str) -> str:
    folder = folder_name or "folders"
    return f"{OFFLINE_CACHE_STATUS_PREFIX} · {account_label} · {folder}"


def load_queued_outbound_message(queue_id: str) -> QueuedOutboundMessage:
    path = os.path.join(outbox_dir(), f"{queue_id}.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid queued message: {queue_id}")
    return QueuedOutboundMessage.from_dict(data)


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
