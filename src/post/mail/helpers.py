# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Folder-tree walking derived from EvolutionMCP (MIT) — see LICENSES/MIT-EvolutionMCP.txt

"""CamelFolderInfo tree walking and message helpers."""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
from typing import Any


class _CamelFolderInfoC(ctypes.Structure):
    """C layout of CamelFolderInfo (PyGObject exposes child/next as raw pointers)."""

    pass


_CamelFolderInfoC._fields_ = [
    ("next", ctypes.c_void_p),
    ("parent", ctypes.c_void_p),
    ("child", ctypes.c_void_p),
    ("full_name", ctypes.c_char_p),
    ("display_name", ctypes.c_char_p),
    ("flags", ctypes.c_uint32),
    ("unread", ctypes.c_int32),
    ("total", ctypes.c_int32),
]


def _folder_field(fi: Any, getter: str, attr: str) -> str | None:
    if hasattr(fi, getter):
        value = getattr(fi, getter)()
        if value is not None and value != "":
            return str(value)
    if hasattr(fi, attr):
        value = getattr(fi, attr)
        if value is not None and value != "":
            return str(value)
    return None


def folder_info_to_dict(fi: Any) -> dict[str, Any]:
    return {
        "full_name": _folder_field(fi, "get_full_name", "full_name"),
        "display_name": _folder_field(fi, "get_display_name", "display_name"),
        "unread": fi.get_unread() if hasattr(fi, "get_unread") else -1,
        "total": fi.get_total() if hasattr(fi, "get_total") else -1,
    }


def _cfi_to_dict(struct: _CamelFolderInfoC) -> dict[str, Any]:
    return {
        "full_name": (
            struct.full_name.decode("utf-8", errors="replace")
            if struct.full_name
            else None
        ),
        "display_name": (
            struct.display_name.decode("utf-8", errors="replace")
            if struct.display_name
            else None
        ),
        "unread": struct.unread,
        "total": struct.total,
    }


def walk_folder_info(fi: Any, results: list[dict[str, Any]]) -> None:
    if fi is None:
        return
    results.append(folder_info_to_dict(fi))
    if fi.child:
        _walk_folder_ptr(int(fi.child), results)
    if fi.next:
        _walk_folder_ptr(int(fi.next), results)


def _walk_folder_ptr(ptr: int, results: list[dict[str, Any]]) -> None:
    while ptr:
        node = ctypes.cast(ptr, ctypes.POINTER(_CamelFolderInfoC)).contents
        results.append(_cfi_to_dict(node))
        if node.child:
            _walk_folder_ptr(node.child, results)
        ptr = node.next


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def message_info_to_dict(info: Any) -> dict[str, Any]:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    date_sent = info.get_date_sent()
    date_recv = info.get_date_received()
    flags = info.get_flags()
    sort_date = date_recv or date_sent or 0
    return {
        "uid": _safe_str(info.get_uid()),
        "subject": _safe_str(info.get_subject()) or "(no subject)",
        "from": _safe_str(info.get_from()) or "",
        "to": _safe_str(info.get_to()) or "",
        "sort_date": sort_date,
        "date_sent": (
            datetime.fromtimestamp(date_sent, tz=timezone.utc).isoformat()
            if date_sent
            else None
        ),
        "date_received": (
            datetime.fromtimestamp(date_recv, tz=timezone.utc).isoformat()
            if date_recv
            else None
        ),
        "size": info.get_size(),
        "flags": {
            "seen": bool(flags & Camel.MessageFlags.SEEN),
            "flagged": bool(flags & Camel.MessageFlags.FLAGGED),
            "deleted": bool(flags & Camel.MessageFlags.DELETED),
        },
    }


def sort_messages_newest_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda message: message.get("sort_date") or 0, reverse=True)


def paginate_messages(
    messages: list[dict[str, Any]], offset: int, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    page = messages[offset : offset + limit]
    has_more = offset + len(page) < len(messages)
    return page, has_more


def extract_message_bodies(mime_msg: Any) -> dict[str, str | None]:
    """Return plain-text and HTML bodies from a Camel.MimeMessage."""
    bodies: dict[str, str | None] = {"plain": None, "html": None}
    _walk_mime_parts(mime_msg, bodies)
    if bodies["plain"] is None and bodies["html"] is None:
        _email_module_fallback(mime_msg, bodies)
    return bodies


def extract_plain_body(mime_msg: Any) -> str | None:
    """Best-effort plain text from a Camel.MimeMessage."""
    return extract_message_bodies(mime_msg)["plain"]


def _walk_mime_parts(part: Any, bodies: dict[str, str | None]) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    content_type = part.get_content_type()
    if content_type is None:
        return

    mime_type = content_type.simple()
    if mime_type.startswith("multipart/"):
        if isinstance(part, Camel.Multipart):
            for i in range(part.get_number()):
                child = part.get_part(i)
                if child is not None:
                    _walk_mime_parts(child, bodies)
            return
        wrapper = part.get_content()
        if wrapper is not None and hasattr(wrapper, "get_number"):
            for i in range(wrapper.get_number()):
                child = wrapper.get_part(i)
                if child is not None:
                    _walk_mime_parts(child, bodies)
        return

    if mime_type == "text/plain" and bodies["plain"] is None:
        bodies["plain"] = _decode_text_part(part)
    elif mime_type == "text/html" and bodies["html"] is None:
        bodies["html"] = _decode_text_part(part)


def _email_module_fallback(mime_msg: Any, bodies: dict[str, str | None]) -> None:
    """Parse raw MIME with Python's email module when Camel walking finds nothing."""
    import email
    import email.policy

    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    try:
        stream = Camel.StreamMem.new()
        mime_msg.write_to_stream_sync(stream, None)
        stream.seek(0, 0)
        raw = stream.get_byte_array()
        if raw is None:
            return
        raw_bytes = bytes(raw) if not isinstance(raw, bytes) else raw
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_content()
            if isinstance(payload, bytes):
                text = payload.decode("utf-8", errors="replace")
            elif isinstance(payload, str):
                text = payload
            else:
                continue
            if ct == "text/plain" and bodies["plain"] is None:
                bodies["plain"] = text
            elif ct == "text/html" and bodies["html"] is None:
                bodies["html"] = text
    except Exception:
        pass


def _decode_text_part(part: Any) -> str | None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    try:
        wrapper = part.get_content()
        if wrapper is not None:
            stream = Camel.StreamMem.new()
            wrapper.decode_to_stream_sync(stream, None)
            stream.seek(0, 0)
            data = stream.get_byte_array()
            if data:
                raw = bytes(data) if not isinstance(data, bytes) else data
                if raw:
                    return raw.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None
