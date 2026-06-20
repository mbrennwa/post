# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Prefix-only message search for the current folder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_BOOLEAN_FIELDS: dict[str, str] = {
    "is:unread": "unread",
    "is:flagged": "flagged",
    "has:attachment": "attachment",
}

# Optional whitespace after ":" so "subject: Auburn" works like "subject:Auburn".
_QUERY_PATTERN = re.compile(
    r"""
    \b(?P<boolean>is:unread|is:flagged|has:attachment)\b
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


@dataclass(frozen=True)
class MessageSearchQuery:
    terms: tuple[SearchTerm, ...]


def parse_search_query(raw: str) -> MessageSearchQuery | None:
    """Parse a prefix-only search string. Returns None if empty or no valid terms."""
    text = raw.strip()
    if not text:
        return None

    terms: list[SearchTerm] = []
    for match in _QUERY_PATTERN.finditer(text):
        boolean = match.group("boolean")
        if boolean is not None:
            field = _BOOLEAN_FIELDS[boolean.lower()]
            terms.append(SearchTerm(field=field, value=None))
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

    if not terms:
        return None
    return MessageSearchQuery(terms=tuple(terms))


def _header_matches(msg: dict[str, Any], field: str, needle: str) -> bool:
    haystack = (msg.get(field) or "").lower()
    return needle.lower() in haystack


def _term_matches(msg: dict[str, Any], term: SearchTerm) -> bool:
    flags = msg.get("flags") or {}

    if term.field == "unread":
        return not flags.get("seen", True)
    if term.field == "flagged":
        return bool(flags.get("flagged"))
    if term.field == "attachment":
        return bool(flags.get("attachments"))

    assert term.value is not None
    return _header_matches(msg, term.field, term.value)


def message_matches(msg: dict[str, Any], query: MessageSearchQuery) -> bool:
    """Return True if the message matches all search terms."""
    return all(_term_matches(msg, term) for term in query.terms)
