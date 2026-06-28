# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Message search for the current folder."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

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
    return any(term.field == "body" for term in query.terms)


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
        return any(
            _contains_insensitive(_header_field(message, header), term.value)
            for header in _TEXT_HEADER_FIELDS
        )
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
) -> list[dict]:
    """Filter folder index messages locally, checking cancellation between body loads."""
    if not query.terms:
        return list(messages)

    needs_body = query_requires_body_scan(query)
    matched: list[dict] = []
    for message in messages:
        if is_cancelled is not None and is_cancelled():
            break
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
    return matched
