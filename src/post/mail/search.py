# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Message search for the current folder."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from post.mail.search_debug import search_debug_enabled, search_trace

SEARCH_FILTER_PROGRESS_INTERVAL = 100
SEARCH_MATCH_BATCH_SIZE = 25

SearchProgressCallback = Callable[["SearchFilterProgress"], None]
SearchMatchCallback = Callable[[list[dict]], None]
SearchCompleteCallback = Callable[[tuple[list[dict], int, int, str]], None]


@dataclass(frozen=True)
class SearchFilterProgress:
    scanned: int
    message_count: int
    matches: int
    folder_label: str | None = None
    folders_done: int | None = None
    folders_total: int | None = None


def make_search_row_key(account_uid: str, folder_name: str, uid: str) -> str:
    return f"{account_uid}\0{folder_name}\0{uid}"


def annotate_search_match(
    message: dict,
    *,
    account_uid: str,
    folder_name: str,
) -> dict:
    uid = str(message.get("uid") or "")
    annotated = dict(message)
    annotated["_search_account_uid"] = account_uid
    annotated["_search_folder"] = folder_name
    annotated["_search_row_key"] = make_search_row_key(account_uid, folder_name, uid)
    return annotated


def format_search_result_meta(
    account_label: str,
    folder_display: str,
    sender: str,
) -> str:
    parts = [part for part in (account_label, folder_display, sender) if part]
    return " · ".join(parts)


def format_search_filter_progress(progress: SearchFilterProgress) -> str:
    if progress.folder_label:
        return f"Searching {progress.folder_label}…"
    return "Searching…"


def search_filter_progress_fraction(progress: SearchFilterProgress) -> float:
    """Return 0.0–1.0 progress for search UI bars."""
    if (
        progress.folders_total is not None
        and progress.folders_total > 0
        and progress.folders_done is not None
    ):
        if progress.message_count > 0:
            within_folder = progress.scanned / progress.message_count
        else:
            within_folder = 1.0
        completed = progress.folders_done - 1 + within_folder
        return min(1.0, completed / progress.folders_total)
    if progress.message_count > 0:
        return progress.scanned / progress.message_count
    return 0.0

# Optional whitespace after ":" so "subject: Auburn" works like "subject:Auburn".
_QUERY_PATTERN = re.compile(
    r"""
    \b(?P<boolean>is|has):\s*(?P<negated>!)?(?P<prop>read|flagged|attachment)\b
    |
    \b(?P<header>from|to|subject|cc|body):\s*
    (?:
        "(?P<quoted>[^"]*)"
        |
        (?P<unquoted>[^\s]+(?:\s+(?!(?:from|to|subject|cc|body|is|has):)[^\s]+)*)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_HEADER_FIELD_NAMES = {
    "from": "From",
    "to": "To",
    "subject": "Subject",
    "cc": "Cc",
}

_TEXT_HEADER_FIELDS = ("Subject", "From", "To", "Cc")


@dataclass(frozen=True)
class SearchTerm:
    field: str
    value: str | None = None
    negated: bool = False


@dataclass(frozen=True)
class MessageSearchQuery:
    terms: tuple[SearchTerm, ...]


def _tokenize_free_text(text: str) -> list[str]:
    try:
        return [token for token in shlex.split(text, posix=True) if token]
    except ValueError:
        return re.findall(r'"[^"]*"|\S+', text)


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


def parse_search_query(raw: str) -> MessageSearchQuery | None:
    """Parse a search string. Bare words match subject/from/to/cc/body; prefixes narrow fields."""
    text = raw.strip()
    if not text:
        return None

    terms: list[SearchTerm] = []
    spans: list[tuple[int, int]] = []

    for match in _QUERY_PATTERN.finditer(text):
        boolean = match.group("boolean")
        if boolean is not None:
            prop = match.group("prop")
            if prop is None:
                continue
            negated = match.group("negated") is not None
            terms.append(SearchTerm(field=prop.lower(), negated=negated))
            spans.append(match.span())
            continue

        header = match.group("header")
        if header is None:
            continue
        value = match.group("quoted")
        if value is None:
            value = match.group("unquoted")
        value = (value or "").strip()
        if not value:
            continue
        terms.append(SearchTerm(field=header.lower(), value=value))
        spans.append(match.span())

    remainder_parts: list[str] = []
    last = 0
    for start, end in sorted(spans):
        if start > last:
            remainder_parts.append(text[last:start])
        last = max(last, end)
    if last < len(text):
        remainder_parts.append(text[last:])
    remainder = " ".join(part.strip() for part in remainder_parts if part.strip())

    for token in _tokenize_free_text(remainder):
        value = _strip_quotes(token).strip()
        if value:
            terms.append(SearchTerm(field="text", value=value))

    if not terms:
        return None
    return MessageSearchQuery(terms=tuple(terms))


def _sexp_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _boolean_term_to_sexp(term: SearchTerm) -> str:
    flag_name = {
        "read": "seen",
        "flagged": "flagged",
        "attachment": "attachments",
    }[term.field]
    flag_expr = f'(system-flag {_sexp_string(flag_name)})'
    if term.negated:
        return f"(not {flag_expr})"
    return flag_expr


def _header_term_to_sexp(header: str, value: str) -> str:
    return f"(header-contains {_sexp_string(header)} {_sexp_string(value)})"


def _text_term_to_sexp(value: str) -> str:
    clauses = [
        _header_term_to_sexp(header, value) for header in _TEXT_HEADER_FIELDS
    ]
    clauses.append(f"(body-contains {_sexp_string(value)})")
    if len(clauses) == 1:
        return clauses[0]
    return f'(or {" ".join(clauses)})'


def _body_term_to_sexp(value: str) -> str:
    return f"(body-contains {_sexp_string(value)})"


def _term_to_sexp(term: SearchTerm) -> str:
    if term.field in ("read", "flagged", "attachment"):
        return _boolean_term_to_sexp(term)
    if term.field == "text":
        assert term.value is not None
        return _text_term_to_sexp(term.value)
    if term.field == "body":
        assert term.value is not None
        return _body_term_to_sexp(term.value)
    assert term.value is not None
    header = _HEADER_FIELD_NAMES[term.field]
    return _header_term_to_sexp(header, term.value)


def query_to_sexp(query: MessageSearchQuery) -> str:
    """Compile a Post search query to a Camel folder search S-expression."""
    clauses = [_term_to_sexp(term) for term in query.terms]
    if not clauses:
        return "(match-all)"
    if len(clauses) == 1:
        inner = clauses[0]
    else:
        inner = f'(and {" ".join(clauses)})'
    return f"(match-all {inner})"


def query_requires_body_scan(query: MessageSearchQuery) -> bool:
    """True when matching needs cached message body text."""
    return any(term.field in ("body", "text") for term in query.terms)


def _header_field(message: dict, field: str) -> str:
    if field == "Subject":
        return str(message.get("subject") or "")
    if field == "From":
        return str(message.get("from") or "")
    if field == "To":
        return str(message.get("to") or "")
    if field == "Cc":
        return str(message.get("cc") or "")
    return ""


def _contains_insensitive(haystack: str, needle: str) -> bool:
    return needle.casefold() in haystack.casefold()


def _message_matches_term(
    message: dict,
    term: SearchTerm,
    *,
    body_text: str | None = None,
) -> bool:
    flags = message.get("flags") or {}
    if term.field == "read":
        seen = bool(flags.get("seen", False))
        return seen if not term.negated else not seen
    if term.field == "flagged":
        flagged = bool(flags.get("flagged", False))
        return flagged if not term.negated else not flagged
    if term.field == "attachment":
        has_attachment = bool(flags.get("attachments", False))
        return has_attachment if not term.negated else not has_attachment

    assert term.value is not None
    if term.field == "body":
        body = body_text or ""
        return _contains_insensitive(body, term.value)
    if term.field == "text":
        if any(
            _contains_insensitive(_header_field(message, header), term.value)
            for header in _TEXT_HEADER_FIELDS
        ):
            return True
        body = body_text or ""
        return _contains_insensitive(body, term.value)
    if term.field in _HEADER_FIELD_NAMES:
        header = _HEADER_FIELD_NAMES[term.field]
        return _contains_insensitive(_header_field(message, header), term.value)
    return False


def filter_messages_by_query(
    messages: list[dict],
    query: MessageSearchQuery,
    *,
    body_text_for_uid: Callable[[str], str | None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: SearchProgressCallback | None = None,
    on_matches: SearchMatchCallback | None = None,
    progress_interval: int = SEARCH_FILTER_PROGRESS_INTERVAL,
    match_batch_size: int = SEARCH_MATCH_BATCH_SIZE,
) -> list[dict]:
    """Filter folder index messages locally, checking cancellation between body loads."""
    if not query.terms:
        return list(messages)

    needs_body = query_requires_body_scan(query)
    matched: list[dict] = []
    pending_batch: list[dict] = []
    message_count = len(messages)

    def flush_matches() -> None:
        if on_matches is not None and pending_batch:
            on_matches(list(pending_batch))
            pending_batch.clear()
    search_trace(
        "filter_messages_begin",
        message_count=message_count,
        term_count=len(query.terms),
        needs_body=needs_body,
    )
    if on_progress is not None and message_count > 0:
        on_progress(SearchFilterProgress(0, message_count, 0))
    for index, message in enumerate(messages):
        if is_cancelled is not None and is_cancelled():
            search_trace(
                "filter_messages_cancelled",
                scanned=index,
                message_count=message_count,
            )
            break
        if (
            on_progress is not None
            and index > 0
            and (index == 1 or index % progress_interval == 0)
        ):
            on_progress(
                SearchFilterProgress(index, message_count, len(matched))
            )
        if search_debug_enabled() and index > 0 and index % progress_interval == 0:
            search_trace(
                "filter_messages_progress",
                scanned=index,
                message_count=message_count,
                matches=len(matched),
            )
        uid = message.get("uid")
        body_text: str | None = None
        if needs_body and body_text_for_uid is not None and uid:
            body_text = body_text_for_uid(str(uid))
            if is_cancelled is not None and is_cancelled():
                break
        if all(
            _message_matches_term(message, term, body_text=body_text)
            for term in query.terms
        ):
            matched.append(message)
            pending_batch.append(message)
            if len(matched) == 1 or len(pending_batch) >= match_batch_size:
                flush_matches()
    flush_matches()
    if on_progress is not None and message_count > 0:
        on_progress(
            SearchFilterProgress(message_count, message_count, len(matched))
        )
    search_trace(
        "filter_messages_done",
        scanned=message_count,
        matches=len(matched),
    )
    return matched
