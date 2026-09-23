# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persist compose drafts when Camel cannot append to Drafts offline."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .compose import ComposeAttachment
from .helpers import format_message_datetime, paginate_messages
from .queue_attachments import load_attachment_sidecars, write_attachment_sidecars

_DRAFT_QUEUE_DIRNAME = "draft-queue"


@dataclass
class QueuedDraft:
    account_uid: str
    drafts_folder_name: str
    to: list[str] | None
    cc: list[str] | None
    bcc: list[str] | None
    subject: str
    body: str
    body_html: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    existing_uid: str | None = None
    queued_at: float = 0.0
    attachments: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueuedDraft:
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
            drafts_folder_name=str(data["drafts_folder_name"]),
            to=list(data["to"]) if data.get("to") is not None else None,
            cc=list(data["cc"]) if data.get("cc") is not None else None,
            bcc=list(data["bcc"]) if data.get("bcc") is not None else None,
            subject=str(data.get("subject") or ""),
            body=str(data.get("body") or ""),
            body_html=(
                str(data["body_html"]) if data.get("body_html") is not None else None
            ),
            in_reply_to=data.get("in_reply_to"),
            references=data.get("references"),
            existing_uid=(
                str(data["existing_uid"])
                if data.get("existing_uid") is not None
                else None
            ),
            queued_at=float(data.get("queued_at") or 0.0),
            attachments=attachments,
        )


def draft_queue_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "post", _DRAFT_QUEUE_DIRNAME)


def _queued_attachment_dir(queue_id: str) -> str:
    return os.path.join(draft_queue_dir(), queue_id)


def new_draft_queue_id() -> str:
    return f"{int(time.time() * 1_000_000)}-{uuid.uuid4().hex}"


def is_queued_draft_id(queue_id: str | None) -> bool:
    if not queue_id:
        return False
    path = os.path.join(draft_queue_dir(), f"{queue_id}.json")
    return os.path.isfile(path)


def load_queued_draft(queue_id: str) -> QueuedDraft:
    path = os.path.join(draft_queue_dir(), f"{queue_id}.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid queued draft: {queue_id}")
    return QueuedDraft.from_dict(data)


def _write_attachment_sidecars(
    queue_id: str,
    attachments: Sequence[ComposeAttachment],
) -> list[dict[str, str]]:
    return write_attachment_sidecars(draft_queue_dir(), queue_id, attachments)


def load_queued_draft_attachments(
    queue_id: str,
    draft: QueuedDraft,
) -> list[ComposeAttachment]:
    return load_attachment_sidecars(
        draft_queue_dir(), queue_id, draft.attachments
    )

def enqueue_draft(
    draft: QueuedDraft,
    *,
    attachment_payloads: Sequence[ComposeAttachment] | None = None,
    queue_id: str | None = None,
) -> str:
    directory = draft_queue_dir()
    os.makedirs(directory, exist_ok=True)
    queue_id = queue_id or new_draft_queue_id()
    if attachment_payloads:
        draft.attachments = _write_attachment_sidecars(queue_id, attachment_payloads)
    payload = draft.to_dict()
    payload["queued_at"] = draft.queued_at or time.time()
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


def list_queued_drafts() -> list[tuple[str, QueuedDraft]]:
    directory = draft_queue_dir()
    if not os.path.isdir(directory):
        return []

    queued: list[tuple[str, QueuedDraft]] = []
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
            queued.append((queue_id, QueuedDraft.from_dict(data)))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    queued.sort(key=lambda item: item[1].queued_at)
    return queued


def remove_queued_draft(queue_id: str) -> None:
    path = os.path.join(draft_queue_dir(), f"{queue_id}.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    attachment_dir = _queued_attachment_dir(queue_id)
    if os.path.isdir(attachment_dir):
        shutil.rmtree(attachment_dir, ignore_errors=True)


def count_queued_drafts() -> int:
    return len(list_queued_drafts())


def list_queued_drafts_for_folder(
    account_uid: str,
    drafts_folder_name: str,
) -> list[tuple[str, QueuedDraft]]:
    """Queued drafts for one account Drafts folder (newest first)."""
    items = [
        (queue_id, draft)
        for queue_id, draft in list_queued_drafts()
        if draft.account_uid == account_uid
        and draft.drafts_folder_name == drafts_folder_name
    ]
    items.sort(key=lambda item: item[1].queued_at, reverse=True)
    return items


def _format_address_field(addrs: list[str] | None) -> str:
    if not addrs:
        return ""
    return ", ".join(addrs)


def _preview_to_text(to_text: str) -> str:
    if len(to_text) <= 60:
        return to_text
    return to_text[:57] + "..."


def queued_draft_to_list_dict(
    queue_id: str,
    draft: QueuedDraft,
    *,
    from_label: str,
) -> dict[str, Any]:
    to_text = _format_address_field(draft.to)
    return {
        "uid": queue_id,
        "subject": draft.subject or "(No subject)",
        "from": from_label,
        "to": to_text,
        "preview_to": _preview_to_text(to_text),
        "sort_date": draft.queued_at,
        "flags": {"seen": True, "draft": True, "queued": True},
        "has_attachments": bool(draft.attachments),
    }


def read_queued_draft(
    queue_id: str,
    *,
    account_uid: str,
    from_label: str,
) -> dict[str, Any]:
    """Load a queued draft as a compose/reader-style message dict."""
    draft = load_queued_draft(queue_id)
    if draft.account_uid != account_uid:
        raise ValueError("Queued draft belongs to another account")
    date_str = (format_message_datetime(draft.queued_at) or "")[:16]
    return {
        "uid": queue_id,
        "subject": draft.subject or "(No subject)",
        "from": from_label,
        "to": _format_address_field(draft.to),
        "cc": _format_address_field(draft.cc),
        "bcc": _format_address_field(draft.bcc),
        "date_sent": date_str,
        "body_plain": draft.body or "",
        "body_html": draft.body_html,
        "in_reply_to": draft.in_reply_to,
        "references": draft.references,
        "flags": {"seen": True, "draft": True, "queued": True},
        "has_attachments": bool(draft.attachments),
    }


def list_queued_draft_messages(
    account_uid: str,
    drafts_folder_name: str,
    *,
    from_label: str,
) -> tuple[list[dict[str, Any]], int, int]:
    items = list_queued_drafts_for_folder(account_uid, drafts_folder_name)
    total = len(items)
    messages = [
        queued_draft_to_list_dict(queue_id, draft, from_label=from_label)
        for queue_id, draft in items
    ]
    return messages, 0, total


def list_queued_draft_messages_page(
    account_uid: str,
    drafts_folder_name: str,
    *,
    from_label: str,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    messages, unread, total = list_queued_draft_messages(
        account_uid,
        drafts_folder_name,
        from_label=from_label,
    )
    page, has_more = paginate_messages(messages, offset, limit)
    return page, unread, total, has_more


def merge_queued_drafts_into_messages(
    messages: list[dict[str, Any]],
    account_uid: str,
    drafts_folder_name: str,
    *,
    from_label: str,
) -> tuple[list[dict[str, Any]], int]:
    """Prepend queued draft rows not already present by uid; return (list, added)."""
    queued, _unread, queued_total = list_queued_draft_messages(
        account_uid,
        drafts_folder_name,
        from_label=from_label,
    )
    if not queued:
        return messages, 0
    existing_uids = {str(msg.get("uid") or "") for msg in messages}
    extra = [row for row in queued if str(row.get("uid") or "") not in existing_uids]
    if not extra:
        return messages, 0
    return extra + list(messages), len(extra)
