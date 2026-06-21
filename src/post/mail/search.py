# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Message search for the current folder."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

# Optional whitespace after ":" so "subject: Auburn" works like "subject:Auburn".
_QUERY_PATTERN = re.compile(
    r"""
    \b(?P<boolean>is|has):\s*(?P<negated>!)?(?P<prop>read|flagged|attachment)\b
    |
    \b(?P<header>from|to|subject|cc):\s*
    (?:
        "(?P<quoted>[^"]*)"
        |
        (?P<unquoted>[^\s]+(?:\s+(?!(?:from|to|subject|cc|is|has):)[^\s]+)*)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class SearchTerm:
    field: str
    value: str | None = None
    negated: bool = False

_DEFAULT_TEXT_FIELDS = ("subject", "from", "to", "cc")


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
    """Parse a search string. Bare words match subject/from/to/cc; prefixes narrow fields."""
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


def _header_matches(msg: dict[str, Any], field: str, needle: str) -> bool:
    haystack = (msg.get(field) or "").lower()
    return needle.lower() in haystack


def _text_matches(msg: dict[str, Any], needle: str) -> bool:
    lower = needle.lower()
    for field in _DEFAULT_TEXT_FIELDS:
        if lower in (msg.get(field) or "").lower():
            return True
    return False


def _boolean_matches(msg: dict[str, Any], field: str) -> bool:
    flags = msg.get("flags") or {}
    if field == "read":
        return bool(flags.get("seen", True))
    if field == "flagged":
        return bool(flags.get("flagged"))
    if field == "attachment":
        return bool(flags.get("attachments"))
    return False


def _term_matches(msg: dict[str, Any], term: SearchTerm) -> bool:
    if term.field in ("read", "flagged", "attachment"):
        result = _boolean_matches(msg, term.field)
        return not result if term.negated else result
    if term.field == "text":
        assert term.value is not None
        return _text_matches(msg, term.value)

    assert term.value is not None
    return _header_matches(msg, term.field, term.value)


def message_matches(msg: dict[str, Any], query: MessageSearchQuery) -> bool:
    """Return True if the message matches all search terms."""
    return all(_term_matches(msg, term) for term in query.terms)
