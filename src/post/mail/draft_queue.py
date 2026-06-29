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


def load_queued_draft_attachments(
    queue_id: str,
    draft: QueuedDraft,
) -> list[ComposeAttachment]:
    if not draft.attachments:
        return []
    directory = _queued_attachment_dir(queue_id)
    loaded: list[ComposeAttachment] = []
    for ref in draft.attachments:
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
