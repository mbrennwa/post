# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Canonical message identity for list selection and the reader (#509).

A MessageId is ``(account_uid, folder_name, camel_uid)``. The on-the-wire /
in-memory key encoding matches search row keys (NUL-separated) so search and
folder views share one identity type.
"""

from __future__ import annotations

from dataclasses import dataclass

from post.mail.search import make_search_row_key, parse_search_row_key


@dataclass(frozen=True)
class MessageId:
    account_uid: str
    folder_name: str
    uid: str

    def key(self) -> str:
        return format_message_id(self.account_uid, self.folder_name, self.uid)

    @classmethod
    def from_parts(
        cls, account_uid: str, folder_name: str, uid: str
    ) -> MessageId | None:
        if not account_uid or not folder_name or not uid:
            return None
        return cls(str(account_uid), str(folder_name), str(uid))

    @classmethod
    def parse(cls, key: str | None) -> MessageId | None:
        if not key:
            return None
        parsed = parse_message_id(key)
        if parsed is None:
            return None
        return cls(*parsed)


def format_message_id(account_uid: str, folder_name: str, uid: str) -> str:
    """Encode a MessageId as the shared NUL-separated list key."""
    return make_search_row_key(account_uid, folder_name, uid)


def parse_message_id(key: str) -> tuple[str, str, str] | None:
    """Return ``(account_uid, folder_name, uid)`` for a MessageId key."""
    return parse_search_row_key(key)


def annotate_message_id(
    message: dict,
    *,
    account_uid: str,
    folder_name: str,
) -> dict:
    """Stamp a folder (or other non-search) row with a canonical MessageId."""
    uid = str(message.get("uid") or "")
    annotated = dict(message)
    annotated["_list_account_uid"] = account_uid
    annotated["_list_folder"] = folder_name
    if uid:
        annotated["_message_id"] = format_message_id(account_uid, folder_name, uid)
    return annotated


def message_list_key(message: dict) -> str:
    """List-row identity: search key, message id, or bare uid fallback."""
    row_key = message.get("_search_row_key") or message.get("_message_id")
    if row_key:
        return str(row_key)
    return str(message.get("uid") or "")


def message_id_from_message(message: dict | None) -> MessageId | None:
    """Resolve a MessageId from an annotated list/reader message dict."""
    if not isinstance(message, dict):
        return None
    key = message.get("_search_row_key") or message.get("_message_id")
    parsed = MessageId.parse(str(key)) if key else None
    if parsed is not None:
        return parsed
    uid = str(message.get("uid") or "")
    if not uid:
        return None
    for account_key, folder_key in (
        ("_search_account_uid", "_search_folder"),
        ("_list_account_uid", "_list_folder"),
    ):
        account_uid = message.get(account_key)
        folder_name = message.get(folder_key)
        if account_uid and folder_name:
            return MessageId.from_parts(str(account_uid), str(folder_name), uid)
    return None


def body_matches_message_id(msg: dict, message_id: MessageId) -> bool:
    """True when a loaded body is the mail identified by ``message_id``.

    Accepts RestId remaps via ``_previous_uid`` (loaded uid is the new id while
    the request still carried the previous uid).
    """
    loaded_uid = str(msg.get("uid") or "")
    if not loaded_uid:
        return False
    if loaded_uid == message_id.uid:
        return True
    previous_uid = str(msg.get("_previous_uid") or "")
    return bool(previous_uid) and previous_uid == message_id.uid
