# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""GNOME HIG capitalization checks for user-visible strings.

See https://developer.gnome.org/hig/guidelines/writing-style.html
"""

from __future__ import annotations

import re

# Articles and prepositions that stay lowercase in the middle of header labels.
_HEADER_LOWER_MIDDLE = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "of",
        "as",
        "and",
        "or",
        "with",
        "all",
    }
)

# Words that stay capitalized in sentence-style running text.
_SENTENCE_PROPER_NOUNS = frozenset(
    {
        "drafts",
        "trash",
        "inbox",
        "outbox",
        "archive",
        "post",
    }
)

_WORD_RE = re.compile(r"^(.+?)([….?!:,;…]*)$", re.UNICODE)


def _split_word(word: str) -> tuple[str, str]:
    match = _WORD_RE.match(word)
    if match is None:
        return word, ""
    return match.group(1), match.group(2)


def _capitalize_core(core: str) -> str:
    if not core:
        return core
    if core.isupper() and len(core) <= 4:
        return core
    return core[0].upper() + core[1:].lower()


def to_header_capitalization(text: str) -> str:
    """Return the GNOME header-capitalization form of *text*."""
    if not text:
        return text
    paren_match = re.match(r"^(.*)(\s+\([^)]+\))$", text)
    if paren_match:
        return to_header_capitalization(paren_match.group(1)) + paren_match.group(2)
    words = text.split()
    if not words:
        return text
    last_index = len(words) - 1
    normalized: list[str] = []
    for index, word in enumerate(words):
        core, punct = _split_word(word)
        lower = core.lower()
        if index == 0 or index == last_index:
            normalized.append(_capitalize_core(core) + punct)
        elif lower in _HEADER_LOWER_MIDDLE:
            normalized.append(lower + punct)
        elif len(core) >= 4:
            normalized.append(_capitalize_core(core) + punct)
        else:
            normalized.append(_capitalize_core(core) + punct)
    return " ".join(normalized)


def is_header_capitalized(text: str) -> bool:
    """Return True when *text* follows GNOME header capitalization."""
    return text == to_header_capitalization(text)


def is_sentence_capitalized(text: str) -> bool:
    """Return True when *text* follows GNOME sentence capitalization."""
    if not text:
        return True
    if not text[0].isupper():
        return False
    return not re.search(r"[.!?]\s+[a-z]", text)


def heading_capitalization_style(text: str) -> str:
    """Return ``header`` or ``sentence`` for dialog heading text."""
    if text.startswith("Could not "):
        return "sentence"
    if text in _INFORMAL_SENTENCE_HEADINGS:
        return "sentence"
    return "header"


_INFORMAL_SENTENCE_HEADINGS = frozenset({"Save draft?"})
