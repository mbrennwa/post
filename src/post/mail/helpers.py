# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later AND LicenseRef-MIT-EvolutionMCP
#
# Folder-tree walking derived from EvolutionMCP (MIT) — see LICENSES/LicenseRef-MIT-EvolutionMCP.txt

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
    flags = fi.get_flags() if hasattr(fi, "get_flags") else 0
    return {
        "full_name": _folder_field(fi, "get_full_name", "full_name"),
        "display_name": _folder_field(fi, "get_display_name", "display_name"),
        "unread": fi.get_unread() if hasattr(fi, "get_unread") else -1,
        "total": fi.get_total() if hasattr(fi, "get_total") else -1,
        "flags": int(flags),
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
        "flags": int(struct.flags),
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


def _valid_unix_timestamp(unix_time: float | int | None) -> float | None:
    """Return unix seconds when in a sane range, otherwise None."""
    if unix_time is None:
        return None
    try:
        value = float(unix_time)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Reject values outside roughly 1970–2100; Camel can return garbage.
    if value > 4102444800:
        return None
    return value


def format_message_datetime(unix_time: float | int | None) -> str | None:
    value = _valid_unix_timestamp(unix_time)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OSError, OverflowError, ValueError):
        return None


def message_info_to_dict(info: Any) -> dict[str, Any]:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    date_sent = info.get_date_sent()
    date_recv = info.get_date_received()
    flags = info.get_flags()
    sort_date = (
        _valid_unix_timestamp(date_recv)
        or _valid_unix_timestamp(date_sent)
        or 0
    )
    return {
        "uid": _safe_str(info.get_uid()),
        "subject": _safe_str(info.get_subject()) or "(no subject)",
        "from": _safe_str(info.get_from()) or "",
        "to": _safe_str(info.get_to()) or "",
        "cc": _safe_str(info.get_cc()) or "",
        "message_id": _safe_str(info.get_message_id())
        if hasattr(info, "get_message_id")
        else None,
        "sort_date": sort_date,
        "date_sent": format_message_datetime(date_sent),
        "date_received": format_message_datetime(date_recv),
        "size": info.get_size(),
        "flags": {
            "seen": bool(flags & Camel.MessageFlags.SEEN),
            "flagged": bool(flags & Camel.MessageFlags.FLAGGED),
            "deleted": bool(flags & Camel.MessageFlags.DELETED),
            "attachments": bool(flags & Camel.MessageFlags.ATTACHMENTS),
        },
    }


def message_is_unread(msg: dict[str, Any]) -> bool:
    flags = msg.get("flags") or {}
    return not flags.get("seen", True)


def message_menu_count_suffix(count: int) -> str:
    return f" ({count})" if count > 1 else ""


def read_menu_items(seen_states: list[bool]) -> list[str]:
    """Return read-menu actions to show: ``read``, ``unread``, or both."""
    if not seen_states:
        return []
    items: list[str] = []
    if not all(seen_states):
        items.append("read")
    if any(seen_states):
        items.append("unread")
    return items


def flag_menu_items(flagged_states: list[bool]) -> list[str]:
    """Return flag-menu actions to show: ``flag``, ``unflag``, or both."""
    if not flagged_states:
        return []
    items: list[str] = []
    if not all(flagged_states):
        items.append("flag")
    if any(flagged_states):
        items.append("unflag")
    return items


def read_menu_label(action: str, count: int) -> str:
    suffix = message_menu_count_suffix(count)
    if action == "read":
        return f"Mark as Read{suffix}"
    return f"Mark as Unread{suffix}"


def flag_menu_label(action: str, count: int) -> str:
    suffix = message_menu_count_suffix(count)
    if action == "flag":
        return f"Flag{suffix}"
    return f"Unflag{suffix}"


def message_is_flagged(msg: dict[str, Any]) -> bool:
    flags = msg.get("flags") or {}
    return bool(flags.get("flagged"))


def message_has_attachments(msg: dict[str, Any]) -> bool:
    flags = msg.get("flags") or {}
    return bool(flags.get("attachments"))


def format_message_list_date(msg: dict[str, Any]) -> str:
    raw = msg.get("date_received") or msg.get("date_sent")
    if raw:
        return raw[:16] if len(raw) >= 16 else raw
    sort_date = msg.get("sort_date")
    valid_sort = _valid_unix_timestamp(sort_date)
    if valid_sort is not None:
        try:
            return datetime.fromtimestamp(valid_sort, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OSError, OverflowError, ValueError):
            return ""
    return ""


def format_message_header(msg: dict[str, Any]) -> str:
    lines = [
        f"From: {msg.get('from', '')}",
        f"To: {msg.get('to', '')}",
    ]
    cc = (msg.get("cc") or "").strip()
    if cc:
        lines.append(f"CC: {cc}")
    date = msg.get("date_received") or msg.get("date_sent") or ""
    lines.append(f"Date: {date}")
    return "\n".join(lines)


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


def extract_attachments(mime_msg: Any) -> list[dict[str, Any]]:
    """Return attachment metadata from a Camel.MimeMessage."""
    attachments, _parts = _collect_attachments(mime_msg)
    return attachments


def get_attachment_data(mime_msg: Any, index: int) -> tuple[str, bytes]:
    """Return (filename, raw bytes) for an attachment by index."""
    attachments, parts = _collect_attachments(mime_msg)
    if index < 0 or index >= len(attachments):
        raise ValueError(f"Attachment not found: {index}")

    meta = attachments[index]
    if meta.get("source") == "email":
        data = _email_attachment_data_by_index(mime_msg, index)
    else:
        data = _decode_attachment_part(parts[index])
    if not data:
        raise ValueError(f"Could not decode attachment: {meta.get('filename')}")
    return meta.get("filename") or "attachment", data


def _collect_attachments(mime_msg: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    attachments: list[dict[str, Any]] = []
    parts: list[Any] = []
    _walk_attachment_parts(mime_msg, attachments, parts)
    if attachments:
        for index, attachment in enumerate(attachments):
            attachment["index"] = index
        return attachments, parts

    _email_collect_attachments(mime_msg, attachments)
    return attachments, [None] * len(attachments)


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


def _walk_attachment_parts(
    part: Any,
    attachments: list[dict[str, Any]],
    parts: list[Any] | None = None,
) -> None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not hasattr(part, "get_content_type"):
        return

    content_type = part.get_content_type()
    if content_type is None:
        return

    mime_type = content_type.simple()
    if mime_type.startswith("multipart/"):
        if isinstance(part, Camel.Multipart):
            for i in range(part.get_number()):
                child = part.get_part(i)
                if child is not None:
                    _walk_attachment_parts(child, attachments, parts)
            return
        wrapper = part.get_content()
        if wrapper is not None and hasattr(wrapper, "get_number"):
            for i in range(wrapper.get_number()):
                child = wrapper.get_part(i)
                if child is not None:
                    _walk_attachment_parts(child, attachments, parts)
        return

    if not _mime_part_is_attachment(part, mime_type):
        return

    filename = part.get_filename() if hasattr(part, "get_filename") else None
    size = part.get_size() if hasattr(part, "get_size") else None
    attachments.append(
        {
            "filename": _safe_str(filename) or "attachment",
            "mime_type": mime_type,
            "size": size if isinstance(size, int) else None,
        }
    )
    if parts is not None:
        parts.append(part)


def _mime_part_is_attachment(part: Any, mime_type: str) -> bool:
    content_type = part.get_content_type() if hasattr(part, "get_content_type") else None
    if hasattr(part, "get_content_disposition"):
        disposition = part.get_content_disposition()
        if disposition is not None and content_type is not None:
            if disposition.is_attachment(content_type):
                return True

    disposition_name = part.get_disposition() if hasattr(part, "get_disposition") else None
    if disposition_name and str(disposition_name).lower() == "attachment":
        return True

    filename = part.get_filename() if hasattr(part, "get_filename") else None
    if filename and mime_type not in ("text/plain", "text/html"):
        return True

    return False


def _email_collect_attachments(mime_msg: Any, attachments: list[dict[str, Any]]) -> None:
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
            disposition = part.get_content_disposition()
            filename = part.get_filename()
            if disposition != "attachment" and not filename:
                continue
            if disposition == "inline" and part.get_content_type().startswith("text/"):
                continue
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "mime_type": part.get_content_type(),
                    "size": len(payload),
                    "source": "email",
                    "index": len(attachments),
                }
            )
    except Exception:
        pass


def _email_attachment_data_by_index(mime_msg: Any, index: int) -> bytes | None:
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
            return None
        raw_bytes = bytes(raw) if not isinstance(raw, bytes) else raw
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        collected: list[bytes] = []
        for part in msg.walk():
            disposition = part.get_content_disposition()
            filename = part.get_filename()
            if disposition != "attachment" and not filename:
                continue
            if disposition == "inline" and part.get_content_type().startswith("text/"):
                continue
            collected.append(part.get_payload(decode=True) or b"")
        if 0 <= index < len(collected):
            return collected[index]
    except Exception:
        pass
    return None


def _decode_attachment_part(part: Any) -> bytes | None:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    try:
        stream = Camel.StreamMem.new()
        wrapper = part.get_content()
        if wrapper is not None:
            wrapper.decode_to_stream_sync(stream, None)
        elif hasattr(part, "decode_to_stream_sync"):
            part.decode_to_stream_sync(stream, None)
        else:
            return None
        stream.seek(0, 0)
        data = stream.get_byte_array()
        if data:
            return bytes(data) if not isinstance(data, bytes) else data
    except Exception:
        pass
    return None


def format_attachment_size(size: int | None) -> str:
    if size is None or size < 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


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
