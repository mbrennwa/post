# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later AND LicenseRef-MIT-EvolutionMCP
#
# Folder-tree walking derived from EvolutionMCP (MIT) — see LICENSES/LicenseRef-MIT-EvolutionMCP.txt

"""CamelFolderInfo tree walking and message helpers."""

from __future__ import annotations

import ctypes
import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

_FORWARD_MARKER = "---------- Forwarded message ---------"
_BLOCKQUOTE_RE = re.compile(r"<blockquote\b", re.IGNORECASE)


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


def _decode_header_value(value: Any) -> str | None:
    """Decode inbound header metadata (RFC 2047 encoded-words, legacy charsets)."""
    from email.header import decode_header, make_header

    if value is None:
        return None
    if isinstance(value, str):
        try:
            return str(make_header(decode_header(value)))
        except (TypeError, ValueError, UnicodeError):
            return value
    if isinstance(value, bytes):
        try:
            return str(make_header(decode_header(value.decode("ascii"))))
        except UnicodeDecodeError:
            return _decode_text_bytes(value, None)
        except (TypeError, ValueError, UnicodeError):
            return _decode_text_bytes(value, None)
    return str(value)


_RFC2231_FILENAME_RE = re.compile(r"^[\w-]+''", re.ASCII)


def _looks_like_rfc2231_filename(value: str) -> bool:
    """True when *value* looks like an RFC 2231/5987 filename parameter."""
    if _RFC2231_FILENAME_RE.match(value):
        return True
    if value.startswith("=?"):
        return False
    return "%" in value


def _decode_attachment_filename(value: Any) -> str | None:
    """Decode inbound attachment filenames (RFC 2047 and RFC 2231 filename*)."""
    from email.utils import decode_rfc2231
    from urllib.parse import unquote

    decoded = _decode_header_value(value)
    if not decoded:
        return None
    if not _looks_like_rfc2231_filename(decoded):
        return decoded
    try:
        charset, _language, encoded = decode_rfc2231(decoded)
    except (TypeError, ValueError):
        return decoded
    if charset:
        return unquote(encoded, encoding=charset, errors="replace")
    return unquote(encoded, errors="replace")


def format_recipient_header(value: Any) -> str:
    """Return a formatted To/Cc/From header string from Camel metadata."""
    if value is None:
        return ""
    if isinstance(value, str):
        return (_decode_header_value(value) or "").strip()
    formatter = getattr(value, "format", None)
    if callable(formatter):
        try:
            formatted = formatter()
            if formatted:
                return str(formatted).strip()
        except (TypeError, ValueError):
            pass
        return ""
    return (_decode_header_value(value) or "").strip()


def _recipient_field_from_mime(mime: Any, field: str) -> str:
    """Return a formatted To/Cc/Bcc value from a Camel MIME message."""
    if hasattr(mime, "get_recipients"):
        formatted = format_recipient_header(mime.get_recipients(field))
        if formatted:
            return formatted
    if hasattr(mime, "get_header"):
        header_name = {"to": "To", "cc": "Cc", "bcc": "Bcc"}[field]
        header = mime.get_header(header_name)
        if header:
            formatted = format_recipient_header(header)
            if formatted:
                return formatted
    return ""


_LIST_UNSUBSCRIBE_ANGLE_RE = re.compile(r"<([^>]+)>")
_ONE_CLICK_POST_TOKEN = "list-unsubscribe=one-click"


def parse_list_unsubscribe_uris(header: str | None) -> list[str]:
    """Extract http(s)/mailto URIs from a List-Unsubscribe header value."""
    if not header:
        return []
    raw = header.strip()
    if not raw:
        return []
    candidates: list[str] = []
    for match in _LIST_UNSUBSCRIBE_ANGLE_RE.finditer(raw):
        uri = match.group(1).strip()
        if uri:
            candidates.append(uri)
    if not candidates:
        for part in raw.split(","):
            uri = part.strip().strip("<>").strip()
            if uri:
                candidates.append(uri)
    return [uri for uri in candidates if _is_allowed_unsubscribe_uri(uri)]


def _is_allowed_unsubscribe_uri(uri: str) -> bool:
    lower = uri.lower()
    return lower.startswith(("https://", "http://", "mailto:"))


def has_one_click_unsubscribe_post(header: str | None) -> bool:
    """True when List-Unsubscribe-Post advertises RFC 8058 one-click."""
    if not header:
        return False
    normalized = " ".join(str(header).split()).lower()
    return _ONE_CLICK_POST_TOKEN in normalized


def unsubscribe_action_from_headers(
    list_unsubscribe: str | None,
    list_unsubscribe_post: str | None = None,
) -> dict[str, str] | None:
    """Resolve a usable unsubscribe action from list headers.

    Returns ``{"kind": "post"|"open", "url": "..."}`` or ``None``.
    One-click POST is only offered for https URLs when List-Unsubscribe-Post
    indicates One-Click.
    """
    uris = parse_list_unsubscribe_uris(list_unsubscribe)
    if not uris:
        return None
    https = [uri for uri in uris if uri.lower().startswith("https://")]
    http = [uri for uri in uris if uri.lower().startswith("http://")]
    mailto = [uri for uri in uris if uri.lower().startswith("mailto:")]
    if has_one_click_unsubscribe_post(list_unsubscribe_post) and https:
        return {"kind": "post", "url": https[0]}
    if https:
        return {"kind": "open", "url": https[0]}
    if http:
        return {"kind": "open", "url": http[0]}
    if mailto:
        return {"kind": "open", "url": mailto[0]}
    return None


def perform_one_click_unsubscribe(url: str, *, timeout: float = 30.0) -> None:
    """POST List-Unsubscribe=One-Click to an https unsubscribe URL (RFC 8058)."""
    import urllib.error
    import urllib.request

    if not url.lower().startswith("https://"):
        raise ValueError("One-click unsubscribe requires an https URL")
    request = urllib.request.Request(
        url,
        data=b"List-Unsubscribe=One-Click",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and int(status) >= 400:
                raise OSError(f"Unsubscribe failed with HTTP {status}")
    except urllib.error.HTTPError as exc:
        raise OSError(f"Unsubscribe failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OSError(f"Unsubscribe request failed: {exc.reason}") from exc


def enrich_message_dict_from_mime(result: dict[str, Any], mime: Any) -> None:
    """Prefer full To/Cc/Bcc from MIME over Camel MessageInfo summaries."""
    for field in ("to", "cc", "bcc"):
        value = _recipient_field_from_mime(mime, field)
        if value:
            result[field] = value
    if hasattr(mime, "get_header"):
        reply_to_header = mime.get_header("Reply-To")
        if reply_to_header:
            stripped = format_recipient_header(reply_to_header)
            if stripped:
                result["reply_to"] = stripped
        list_unsubscribe = _decode_header_value(mime.get_header("List-Unsubscribe"))
        list_unsubscribe_post = _decode_header_value(
            mime.get_header("List-Unsubscribe-Post")
        )
        action = unsubscribe_action_from_headers(
            list_unsubscribe, list_unsubscribe_post
        )
        if action is not None:
            result["unsubscribe"] = action


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


def _format_unix_timestamp_local(unix_time: float, fmt: str) -> str:
    return datetime.fromtimestamp(unix_time, tz=timezone.utc).astimezone().strftime(
        fmt
    )


def format_message_datetime(unix_time: float | int | None) -> str | None:
    value = _valid_unix_timestamp(unix_time)
    if value is None:
        return None
    try:
        return _format_unix_timestamp_local(value, "%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return None


def _rfc_message_id_from_message_info(info: Any) -> str | None:
    """Return the RFC 5322 Message-ID header from Camel summary metadata.

    ``MessageInfo.get_message_id()`` is Camel's uint summary hash, not the
    header string — callers must not treat that hash as a Message-ID (#267).
    """
    headers = None
    getter = getattr(info, "get_headers", None)
    if callable(getter):
        try:
            headers = getter()
        except (TypeError, ValueError, AttributeError):
            headers = None
    if headers is not None:
        get_length = getattr(headers, "get_length", None)
        get_name = getattr(headers, "get_name", None)
        get_value = getattr(headers, "get_value", None)
        if (
            callable(get_length)
            and callable(get_name)
            and callable(get_value)
        ):
            try:
                length = get_length()
            except (TypeError, ValueError):
                length = None
            if isinstance(length, int) and length > 0:
                for index in range(length):
                    try:
                        name = get_name(index)
                    except (TypeError, ValueError, IndexError):
                        continue
                    if not isinstance(name, str) or name.lower() != "message-id":
                        continue
                    try:
                        return _decode_header_value(get_value(index))
                    except (TypeError, ValueError, IndexError):
                        return None
    user_header = getattr(info, "get_user_header", None)
    if callable(user_header):
        for name in ("Message-ID", "Message-Id", "message-id"):
            try:
                value = user_header(name)
            except (TypeError, ValueError):
                continue
            if value:
                return _decode_header_value(value)
    return None


def _message_id_hash_from_message_info(info: Any) -> int | None:
    """Return Camel's non-zero summary Message-ID hash, if available."""
    getter = getattr(info, "get_message_id", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        return None
    return value


def message_info_to_dict(
    info: Any, *, uid: str | None = None, backend: str | None = None
) -> dict[str, Any]:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    from post.mail.message_flags import message_info_is_flagged

    date_sent = info.get_date_sent()
    date_recv = info.get_date_received()
    flags = info.get_flags()
    sort_date = (
        _valid_unix_timestamp(date_recv)
        or _valid_unix_timestamp(date_sent)
        or 0
    )
    if uid is None:
        try:
            uid = _decode_header_value(info.get_uid())
        except UnicodeDecodeError:
            uid = None
    return {
        "uid": uid,
        "subject": _decode_header_value(info.get_subject()) or "(no subject)",
        "from": format_recipient_header(info.get_from()),
        "to": format_recipient_header(info.get_to()),
        "cc": format_recipient_header(info.get_cc()),
        # RFC Message-ID header string (not Camel's uint hash — see #267).
        "message_id": _rfc_message_id_from_message_info(info),
        "message_id_hash": _message_id_hash_from_message_info(info),
        "sort_date": sort_date,
        "date_sent": format_message_datetime(date_sent),
        "date_received": format_message_datetime(date_recv),
        "size": info.get_size(),
        "flags": {
            "seen": bool(flags & Camel.MessageFlags.SEEN),
            "flagged": message_info_is_flagged(info, backend=backend),
            "deleted": bool(flags & Camel.MessageFlags.DELETED),
            "attachments": bool(flags & Camel.MessageFlags.ATTACHMENTS),
        },
    }


def message_is_unread(msg: dict[str, Any]) -> bool:
    flags = msg.get("flags") or {}
    return not flags.get("seen", True)


def message_menu_count_suffix(count: int) -> str:
    return f" ({count})" if count > 1 else ""


def uniform_bool_state(states: list[bool]) -> bool | None:
    """Return the shared value when all *states* match; otherwise ``None``.

    Empty or mixed lists return ``None``. Used by header toggles and by
    ``read_menu_items`` / ``flag_menu_items`` for context-menu actions.
    """
    if not states:
        return None
    first = states[0]
    if any(state != first for state in states[1:]):
        return None
    return first


def read_menu_items(seen_states: list[bool]) -> list[str]:
    """Return read-menu actions to show: ``read``, ``unread``, or both."""
    uniform = uniform_bool_state(seen_states)
    if uniform is True:
        return ["unread"]
    if uniform is False:
        return ["read"]
    if not seen_states:
        return []
    return ["read", "unread"]


def flag_menu_items(flagged_states: list[bool]) -> list[str]:
    """Return flag-menu actions to show: ``flag``, ``unflag``, or both."""
    uniform = uniform_bool_state(flagged_states)
    if uniform is True:
        return ["unflag"]
    if uniform is False:
        return ["flag"]
    if not flagged_states:
        return []
    return ["flag", "unflag"]


def should_offer_send_again(*, selection_count: int, source_is_sent: bool) -> bool:
    """Return True when the message context menu should offer Send Again."""
    return selection_count == 1 and source_is_sent


def reader_toggle_button_state(flags: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return reader toolbar read/flag toggle presentation for *flags*.

    Buttons show the action that will run on click, not the current state.
    """
    seen = flags.get("seen", True)
    flagged = flags.get("flagged", False)
    return {
        "read": {
            "icon": "mail-unread-symbolic" if seen else "mail-mark-read-symbolic",
            "tooltip": "Mark as Unread" if seen else "Mark as Read",
            "action_class": "message-read-action",
            "styled_action": not seen,
        },
        "flag": {
            "icon": "mail-flag-symbolic",
            "tooltip": "Unflag" if flagged else "Flag",
            "action_class": "message-flagged",
            "styled_action": not flagged,
        },
    }


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


def message_is_read_unflagged(msg: dict[str, Any]) -> bool:
    flags = msg.get("flags") or {}
    return bool(flags.get("seen", True)) and not bool(flags.get("flagged"))


def message_matches_bulk_archive_scope(msg: dict[str, Any], scope: str) -> bool:
    """Return True when *msg* should be archived for sidebar bulk-archive *scope*.

    Scopes: ``all``, ``read``, ``read_unflagged``.
    """
    if scope == "all":
        return True
    if scope == "read":
        return not message_is_unread(msg)
    if scope == "read_unflagged":
        return message_is_read_unflagged(msg)
    return False


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
            return _format_unix_timestamp_local(valid_sort, "%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return ""
    return ""


def _reply_to_differs_from_from(from_header: str, reply_to_header: str) -> bool:
    """True when Reply-To addresses are not the same set as From."""
    from .compose import normalize_email, parse_address_header

    from_addrs = {
        normalize_email(address)
        for address in parse_address_header(from_header)
    }
    reply_addrs = {
        normalize_email(address)
        for address in parse_address_header(reply_to_header)
    }
    from_addrs.discard("")
    reply_addrs.discard("")
    if not reply_addrs:
        return False
    return reply_addrs != from_addrs


def _format_header_lines(msg: dict[str, Any], *, include_bcc: bool) -> list[str]:
    lines = [
        f"From: {msg.get('from', '')}",
    ]
    reply_to = (msg.get("reply_to") or "").strip()
    if reply_to and _reply_to_differs_from_from(msg.get("from", ""), reply_to):
        lines.append(f"Reply-To: {reply_to}")
    lines.append(f"To: {msg.get('to', '')}")
    cc = (msg.get("cc") or "").strip()
    if cc:
        lines.append(f"Cc: {cc}")
    if include_bcc:
        bcc = (msg.get("bcc") or "").strip()
        if bcc:
            lines.append(f"Bcc: {bcc}")
    date = msg.get("date_received") or msg.get("date_sent") or ""
    lines.append(f"Date: {date}")
    subject = (msg.get("subject") or "").strip()
    if subject:
        lines.append(f"Subject: {subject}")
    return lines


def format_reader_header(msg: dict[str, Any]) -> str:
    """Reader meta line; includes Bcc when present (Sent/Drafts)."""
    return "\n".join(_format_header_lines(msg, include_bcc=True))


def format_forward_quote_header(msg: dict[str, Any]) -> str:
    """Forwarded-message preamble; never includes Bcc."""
    return "\n".join(_format_header_lines(msg, include_bcc=False))


def format_from_search_query(email: str) -> str:
    """Build a ``from: `` search string for *email* (quote when needed)."""
    value = (email or "").strip()
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or '"' in value or "\\" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'from: "{escaped}"'
    return f"from: {value}"


def bare_email_from_address(display_or_email: str) -> str:
    """Return a lowercase bare email, or empty string when none can be parsed."""
    from .compose import normalize_email

    return normalize_email(display_or_email)


def mailto_primary_email(uri: str) -> str:
    """Return the first bare To address from a mailto URI, or empty string."""
    from .compose import normalize_email
    from .mailto import parse_mailto_uri

    try:
        mailto = parse_mailto_uri(uri)
    except ValueError:
        return ""
    if not mailto.to:
        return ""
    return normalize_email(mailto.to[0])


@dataclass(frozen=True)
class ReaderHeaderRow:
    """One reader meta row: address field or plain text (Date)."""

    label: str
    addresses: tuple[str, ...] = ()
    plain: str | None = None


def reader_header_rows(msg: dict[str, Any]) -> list[ReaderHeaderRow]:
    """Structured reader header rows for interactive address widgets."""
    from .compose import parse_address_header

    rows: list[ReaderHeaderRow] = []

    def add_address_field(label: str, raw: str) -> None:
        displays = tuple(parse_address_header(raw))
        if displays:
            rows.append(ReaderHeaderRow(label=label, addresses=displays))
        elif (raw or "").strip():
            rows.append(ReaderHeaderRow(label=label, plain=raw.strip()))

    add_address_field("From", msg.get("from") or "")
    reply_to = (msg.get("reply_to") or "").strip()
    if reply_to and _reply_to_differs_from_from(msg.get("from", ""), reply_to):
        add_address_field("Reply-To", reply_to)
    add_address_field("To", msg.get("to") or "")
    cc = (msg.get("cc") or "").strip()
    if cc:
        add_address_field("Cc", cc)
    bcc = (msg.get("bcc") or "").strip()
    if bcc:
        add_address_field("Bcc", bcc)
    date = msg.get("date_received") or msg.get("date_sent") or ""
    if date:
        rows.append(ReaderHeaderRow(label="Date", plain=str(date)))
    return rows


def sort_messages_newest_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda message: message.get("sort_date") or 0, reverse=True)


def paginate_messages(
    messages: list[dict[str, Any]], offset: int, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    page = messages[offset : offset + limit]
    has_more = offset + len(page) < len(messages)
    return page, has_more


def plain_body_looks_truncated(plain: str, html: str | None) -> bool:
    """True when text/plain likely omits quoted history present in HTML."""
    if not html:
        return False
    plain = plain.strip()
    if not plain:
        return True
    if _FORWARD_MARKER in plain or " wrote:" in plain or re.search(r"^>", plain, re.MULTILINE):
        return False
    return _BLOCKQUOTE_RE.search(html) is not None


_NON_VISIBLE_HTML_CONTAINER_TAGS = frozenset(
    {"head", "noscript", "script", "style", "title"}
)
_STRIP_NON_VISIBLE_HTML_BLOCKS = re.compile(
    r"<(style|script)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


class _QuotableHtmlParser(HTMLParser):
    """Convert HTML message bodies into plain text with blockquote depth markers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._blockquote_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _NON_VISIBLE_HTML_CONTAINER_TAGS:
            self._skip_depth += 1
            return
        if tag == "blockquote":
            if self._parts and not self._parts[-1].endswith("\n"):
                self._parts.append("\n")
            self._blockquote_depth += 1
        elif tag == "br":
            self._parts.append("\n")
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            if self._parts and not self._parts[-1].endswith("\n"):
                self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _NON_VISIBLE_HTML_CONTAINER_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "blockquote":
            self._blockquote_depth = max(0, self._blockquote_depth - 1)
            self._parts.append("\n")
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not data or self._skip_depth > 0:
            return
        if self._blockquote_depth <= 0:
            self._parts.append(data)
            return
        prefix = ">" * self._blockquote_depth + " "
        lines = data.splitlines()
        if len(lines) == 1:
            self._parts.append(f"{prefix}{lines[0]}")
            return
        prefixed = [f"{prefix}{line}" if line else ">" * self._blockquote_depth for line in lines]
        self._parts.append("\n".join(prefixed))


def html_to_quotable_plain(body_html: str) -> str:
    """Best-effort HTML → plain text, preserving nested blockquotes as ``>`` lines."""
    parser = _QuotableHtmlParser()
    try:
        parser.feed(body_html)
        parser.close()
    except Exception:
        cleaned = _STRIP_NON_VISIBLE_HTML_BLOCKS.sub("", body_html)
        text = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()
    text = "".join(parser._parts)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_DATA_URI_BASE64 = re.compile(
    r"data:[^;]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)
_DATA_URI_CSS_URL = re.compile(
    r"url\(\s*['\"]?data:[^)'\"]+['\"]?\s*\)",
    re.IGNORECASE,
)
_SEARCH_URL_PATTERN = re.compile(
    r"https?://[^\s<>'\"\)]+|mailto:[^\s<>'\"\)]+|www\.[^\s<>'\"\)]+",
    re.IGNORECASE,
)


def _strip_search_body_noise(text: str) -> str:
    text = _DATA_URI_BASE64.sub(" ", text)
    text = _DATA_URI_CSS_URL.sub(" ", text)
    text = _SEARCH_URL_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _plain_body_looks_like_html(text: str) -> bool:
    return "<" in text and ">" in text and re.search(r"<\s*[a-zA-Z]", text) is not None


def searchable_body_text(
    *,
    plain: str | None = None,
    html: str | None = None,
) -> str:
    """Return human-readable body text for folder search."""
    if html and html.strip():
        cleaned = _DATA_URI_BASE64.sub("", html)
        cleaned = _DATA_URI_CSS_URL.sub("", cleaned)
        return _strip_search_body_noise(html_to_quotable_plain(cleaned))
    if plain and plain.strip():
        body = plain.strip()
        if _plain_body_looks_like_html(body):
            cleaned = _DATA_URI_BASE64.sub("", body)
            cleaned = _DATA_URI_CSS_URL.sub("", cleaned)
            return _strip_search_body_noise(html_to_quotable_plain(cleaned))
        return _strip_search_body_noise(body)
    return ""


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


def extract_attachments_from_email_message(msg: Any) -> list[dict[str, Any]]:
    """Return attachment metadata from a Python ``email.message`` (tests)."""
    from post.mail.calendar_invite import (
        default_calendar_filename,
        email_part_counts_as_attachment,
        is_calendar_mime,
    )

    attachments: list[dict[str, Any]] = []
    for part in msg.walk():
        if not email_part_counts_as_attachment(part):
            continue
        mime_type = part.get_content_type()
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        display_name = _decode_attachment_filename(filename)
        if not display_name and is_calendar_mime(mime_type):
            display_name = default_calendar_filename(mime_type)
        attachments.append(
            {
                "filename": display_name or "attachment",
                "mime_type": mime_type,
                "size": len(payload),
                "source": "email",
                "index": len(attachments),
            }
        )
    return attachments


def _normalize_content_id(content_id: str) -> str:
    cid = content_id.strip()
    if cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1]
    return cid


def extract_inline_images(mime_msg: Any) -> dict[str, tuple[str, bytes]]:
    """Return Content-ID -> (mime_type, raw bytes) for inline image parts."""
    images: dict[str, tuple[str, bytes]] = {}
    _walk_inline_image_parts(mime_msg, images)
    if not images:
        _email_collect_inline_images(mime_msg, images)
    return images


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


def _mime_message_raw_bytes(mime_msg: Any) -> bytes | None:
    """Serialize a MIME message to raw bytes when Camel allows it."""
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    if not hasattr(mime_msg, "write_to_stream_sync"):
        return None

    if isinstance(mime_msg, Camel.MimeMessage) and not _mime_message_can_serialize(
        mime_msg
    ):
        return None

    try:
        stream = Camel.StreamMem.new()
        if not mime_msg.write_to_stream_sync(stream, None):
            return None
        stream.seek(0, 0)
        raw = stream.get_byte_array()
        if raw is None:
            return None
        raw_bytes = bytes(raw) if not isinstance(raw, bytes) else raw
        return raw_bytes or None
    except Exception:
        return None


def _mime_message_can_serialize(mime_msg: Any) -> bool:
    import gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    content_type = mime_msg.get_content_type()
    if content_type is None:
        return False

    mime_type = content_type.simple()
    if mime_type.startswith("multipart/"):
        if isinstance(mime_msg, Camel.Multipart):
            return mime_msg.get_number() > 0
        wrapper = mime_msg.get_content()
        if wrapper is None or not hasattr(wrapper, "get_number"):
            return False
        return wrapper.get_number() > 0

    return mime_msg.get_content() is not None


def _walk_inline_image_parts(
    part: Any, images: dict[str, tuple[str, bytes]]
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
                    _walk_inline_image_parts(child, images)
            return
        wrapper = part.get_content()
        if wrapper is not None and hasattr(wrapper, "get_number"):
            for i in range(wrapper.get_number()):
                child = wrapper.get_part(i)
                if child is not None:
                    _walk_inline_image_parts(child, images)
        return

    if not mime_type.startswith("image/"):
        return

    content_id = part.get_content_id() if hasattr(part, "get_content_id") else None
    if not content_id and hasattr(part, "get_header"):
        content_id = part.get_header("Content-ID")
    if not content_id:
        return

    data = _decode_attachment_part(part)
    if not data:
        return

    images[_normalize_content_id(str(content_id))] = (mime_type, data)


def _charset_from_camel_part(part: Any) -> str | None:
    content_type = part.get_content_type() if hasattr(part, "get_content_type") else None
    if content_type is None:
        return None
    try:
        charset = content_type.param("charset")
    except (TypeError, ValueError, AttributeError):
        return None
    if charset is None or charset == "":
        return None
    return str(charset)


def _decode_text_bytes(raw: bytes, charset: str | None = None) -> str:
    encodings: list[str] = []
    if charset:
        normalized = charset.strip().strip('"').strip("'")
        if normalized:
            encodings.append(normalized)
    for fallback in ("utf-8", "latin-1"):
        if fallback not in encodings:
            encodings.append(fallback)
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except LookupError:
            continue
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _email_part_text(part: Any) -> str | None:
    try:
        payload = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        charset = part.get_content_charset()
        return _decode_text_bytes(payload, charset)

    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        charset = part.get_content_charset()
        return _decode_text_bytes(payload, charset)
    return None


def _email_module_fallback(mime_msg: Any, bodies: dict[str, str | None]) -> None:
    """Parse raw MIME with Python's email module when Camel walking finds nothing."""
    import email
    import email.policy

    raw_bytes = _mime_message_raw_bytes(mime_msg)
    if raw_bytes is None:
        return

    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    except Exception:
        return

    for part in msg.walk():
        ct = part.get_content_type()
        if ct.startswith("multipart/"):
            continue
        try:
            text = _email_part_text(part)
        except Exception:
            continue
        if text is None:
            continue
        if ct == "text/plain" and bodies["plain"] is None:
            bodies["plain"] = text
        elif ct == "text/html" and bodies["html"] is None:
            bodies["html"] = text


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

    from post.mail.calendar_invite import default_calendar_filename, is_calendar_mime

    filename = part.get_filename() if hasattr(part, "get_filename") else None
    display_name = _decode_attachment_filename(filename)
    if not display_name and is_calendar_mime(mime_type):
        display_name = default_calendar_filename(mime_type)
    calendar_method = None
    if is_calendar_mime(mime_type) and content_type is not None:
        try:
            calendar_method = content_type.param("method")
        except (TypeError, ValueError, AttributeError):
            calendar_method = None
    size = part.get_size() if hasattr(part, "get_size") else None
    meta: dict[str, Any] = {
        "filename": display_name or "attachment",
        "mime_type": mime_type,
        "size": size if isinstance(size, int) else None,
    }
    if calendar_method:
        meta["calendar_method"] = str(calendar_method)
    attachments.append(meta)
    if parts is not None:
        parts.append(part)


def _mime_part_is_attachment(part: Any, mime_type: str) -> bool:
    from post.mail.calendar_invite import is_calendar_mime

    if is_calendar_mime(mime_type):
        return True

    disposition_name = part.get_disposition() if hasattr(part, "get_disposition") else None
    disposition_lower = str(disposition_name).lower() if disposition_name else None
    if mime_type.startswith("image/") and disposition_lower != "attachment":
        content_id = part.get_content_id() if hasattr(part, "get_content_id") else None
        if content_id or disposition_lower == "inline":
            # CID / inline images are shown in the body, not the attachment list (#258).
            return False

    content_type = part.get_content_type() if hasattr(part, "get_content_type") else None
    if hasattr(part, "get_content_disposition"):
        disposition = part.get_content_disposition()
        if disposition is not None and content_type is not None:
            if disposition.is_attachment(content_type):
                return True

    if disposition_lower == "attachment":
        return True

    filename = part.get_filename() if hasattr(part, "get_filename") else None
    if filename and mime_type not in ("text/plain", "text/html"):
        return True

    return False


def _email_collect_inline_images(
    mime_msg: Any, images: dict[str, tuple[str, bytes]]
) -> None:
    import email
    import email.policy

    raw_bytes = _mime_message_raw_bytes(mime_msg)
    if raw_bytes is None:
        return

    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        for part in msg.walk():
            content_id = part.get("Content-ID")
            if not content_id:
                continue
            mime_type = part.get_content_type()
            if not mime_type.startswith("image/"):
                continue
            payload = part.get_payload(decode=True) or b""
            if not payload:
                continue
            images[_normalize_content_id(str(content_id))] = (mime_type, payload)
    except Exception:
        pass


def _email_collect_attachments(mime_msg: Any, attachments: list[dict[str, Any]]) -> None:
    import email
    import email.policy

    from post.mail.calendar_invite import (
        default_calendar_filename,
        email_part_counts_as_attachment,
        is_calendar_mime,
    )

    raw_bytes = _mime_message_raw_bytes(mime_msg)
    if raw_bytes is None:
        return

    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        for part in msg.walk():
            if not email_part_counts_as_attachment(part):
                continue
            mime_type = part.get_content_type()
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            display_name = _decode_attachment_filename(filename)
            if not display_name and is_calendar_mime(mime_type):
                display_name = default_calendar_filename(mime_type)
            attachments.append(
                {
                    "filename": display_name or "attachment",
                    "mime_type": mime_type,
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

    from post.mail.calendar_invite import email_part_counts_as_attachment

    raw_bytes = _mime_message_raw_bytes(mime_msg)
    if raw_bytes is None:
        return None

    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        collected: list[bytes] = []
        for part in msg.walk():
            if not email_part_counts_as_attachment(part):
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


def write_temp_attachment(filename: str, data: bytes) -> str:
    """Write attachment bytes to a temp file and return its path."""
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    directory = os.path.join(GLib.get_tmp_dir(), "post")
    os.makedirs(directory, exist_ok=True)
    basename = os.path.basename(filename.replace("/", "_").replace("\\", "_")) or "attachment"
    path = os.path.join(directory, basename)
    if os.path.exists(path):
        stem, ext = os.path.splitext(basename)
        counter = 1
        while os.path.exists(path):
            path = os.path.join(directory, f"{stem}-{counter}{ext}")
            counter += 1
    with open(path, "wb") as handle:
        handle.write(data)
    return path


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
                    return _decode_text_bytes(raw, _charset_from_camel_part(part))
    except Exception:
        pass
    return None
