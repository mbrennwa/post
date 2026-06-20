# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Correspondent extraction and address autocomplete helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .compose import _ADDRESS_SPLIT, normalize_email, parse_address_header

_NAME_FROM_DISPLAY = re.compile(r'^(.+?)\s*<[^>]+>\s*$')


@dataclass(frozen=True)
class Correspondent:
    display: str
    email: str
    name: str


def _name_from_display(display: str) -> str:
    match = _NAME_FROM_DISPLAY.match(display.strip())
    if match is None:
        return ""
    return match.group(1).strip().strip('"')


def _correspondent_from_display(display: str) -> Correspondent | None:
    email = normalize_email(display)
    if not email or "@" not in email:
        return None
    return Correspondent(
        display=display,
        email=email,
        name=_name_from_display(display),
    )


def collect_correspondents(
    messages: list[dict[str, Any]],
    *,
    exclude_emails: set[str] | None = None,
) -> list[Correspondent]:
    """Collect unique correspondents from message headers, preserving first-seen order."""
    excluded = {email.casefold() for email in (exclude_emails or set()) if email}
    seen: set[str] = set()
    correspondents: list[Correspondent] = []

    for message in messages:
        for header in ("from", "to", "cc"):
            raw = message.get(header) or ""
            for display in parse_address_header(raw):
                correspondent = _correspondent_from_display(display)
                if correspondent is None:
                    continue
                if correspondent.email in excluded or correspondent.email in seen:
                    continue
                seen.add(correspondent.email)
                correspondents.append(correspondent)

    return correspondents


def current_address_token(text: str) -> str:
    """Return the active recipient token after the last comma."""
    text = text or ""
    parts = _ADDRESS_SPLIT.split(text)
    if not parts:
        return text.strip()
    return parts[-1].strip()


def _matches_prefix(value: str, prefix: str) -> bool:
    return value.casefold().startswith(prefix.casefold())


def correspondent_matches_prefix(correspondent: Correspondent, prefix: str) -> bool:
    """Return whether a single correspondent matches a typed prefix."""
    prefix = prefix.strip()
    if not prefix:
        return False
    if correspondent.name and _matches_prefix(correspondent.name, prefix):
        return True
    if _matches_prefix(correspondent.email, prefix):
        return True
    return _matches_prefix(correspondent.display, prefix)


def match_correspondents(
    candidates: list[Correspondent],
    prefix: str,
    *,
    limit: int = 10,
) -> list[Correspondent]:
    """Return prefix matches on name, email, or display string."""
    prefix = prefix.strip()
    if not prefix:
        return []

    name_matches: list[Correspondent] = []
    email_matches: list[Correspondent] = []
    display_matches: list[Correspondent] = []

    for candidate in candidates:
        if candidate.name and _matches_prefix(candidate.name, prefix):
            name_matches.append(candidate)
        elif _matches_prefix(candidate.email, prefix):
            email_matches.append(candidate)
        elif _matches_prefix(candidate.display, prefix):
            display_matches.append(candidate)

    ranked = name_matches + email_matches + display_matches
    return ranked[:limit]


def apply_address_completion(full_text: str, selected_display: str) -> str:
    """Replace the current token with the selected address."""
    token = current_address_token(full_text)
    if token:
        prefix = full_text[: len(full_text) - len(token)]
    else:
        prefix = full_text
    return f"{prefix}{selected_display}, "
