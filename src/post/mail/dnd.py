# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Drag-and-drop payload helpers for moving messages between folders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MESSAGE_TRANSFER_MIME = "application/x-post-message-transfer"


@dataclass(frozen=True)
class MessageTransferPayload:
    account_uid: str
    source_folder: str
    uids: tuple[str, ...]


def encode_message_transfer(payload: MessageTransferPayload) -> bytes:
    return json.dumps(
        {
            "account_uid": payload.account_uid,
            "source_folder": payload.source_folder,
            "uids": list(payload.uids),
        }
    ).encode("utf-8")


def decode_message_transfer(data: bytes) -> MessageTransferPayload | None:
    try:
        raw: dict[str, Any] = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    account_uid = raw.get("account_uid")
    source_folder = raw.get("source_folder")
    uids = raw.get("uids")
    if not isinstance(account_uid, str) or not account_uid:
        return None
    if not isinstance(source_folder, str) or not source_folder:
        return None
    if not isinstance(uids, list) or not uids:
        return None
    if not all(isinstance(uid, str) and uid for uid in uids):
        return None
    return MessageTransferPayload(
        account_uid=account_uid,
        source_folder=source_folder,
        uids=tuple(uids),
    )


def validate_message_drop(
    payload: MessageTransferPayload,
    *,
    dest_account_uid: str,
    dest_folder: str,
    dest_is_outbox: bool,
) -> bool:
    if dest_is_outbox:
        return False
    if payload.account_uid != dest_account_uid:
        return False
    if payload.source_folder == dest_folder:
        return False
    return True
