# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reply / forward subject prefixes (case-insensitive).

Strategy: strip known localized reply/forward prefixes (stacked), then always
re-prefix with English ``Re:`` / ``Fwd:``. No account/UI locale chooses the
outgoing prefix; the lists are pragmatic MUA coverage, not full i18n.
Mixed stacks like "AW: Re: Fwd: topic" collapse cleanly.
"""

from __future__ import annotations

REPLY_SUBJECT_PREFIXES = (
    "re",
    "aw",
    "sv",
    "antw",
    "antwort",
    "res",
    "rif",
    "rif.",
    "odp",
    "ynt",
)
FORWARD_SUBJECT_PREFIXES = (
    "fwd",
    "fw",
    "wg",
    "vl",
    "vs",
    "tr",
    "enc",
    "rv",
)
_SUBJECT_PREFIXES = tuple(
    sorted(
        {*REPLY_SUBJECT_PREFIXES, *FORWARD_SUBJECT_PREFIXES},
        key=len,
        reverse=True,
    )
)
_FORWARD_PREFIX_SET = frozenset(FORWARD_SUBJECT_PREFIXES)


def strip_subject_prefixes(subject: str) -> str:
    """Remove stacked reply/forward prefixes from the start of a subject."""
    text = subject
    while True:
        stripped = text.lstrip()
        lowered = stripped.lower()
        matched = False
        for prefix in _SUBJECT_PREFIXES:
            token = f"{prefix}:"
            if lowered.startswith(token):
                text = stripped[len(token) :]
                matched = True
                break
        if not matched:
            return stripped


def leftmost_subject_prefix(subject: str | None) -> str | None:
    """Return the first reply/forward prefix, or None if the subject has none."""
    stripped = (subject or "").lstrip()
    lowered = stripped.lower()
    for prefix in _SUBJECT_PREFIXES:
        token = f"{prefix}:"
        if lowered.startswith(token):
            return prefix
    return None


def subject_looks_like_forward(subject: str | None) -> bool:
    """True when the leftmost subject prefix is a forward prefix (Fwd/Fw/WG/…)."""
    prefix = leftmost_subject_prefix(subject)
    return prefix in _FORWARD_PREFIX_SET
