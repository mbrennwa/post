# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Correspondent extraction and address autocomplete helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .compose import _ADDRESS_SPLIT, normalize_email
from .folders import (
    is_drafts_folder_name,
    is_post_local_folder,
    is_post_outbox_folder,
    is_virtual_folder,
)
from .message_list_state import is_junk_folder_name

_NAME_FROM_DISPLAY = re.compile(r'^(.+?)\s*<[^>]+>\s*$')
_ANGLE_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_BARE_EMAIL = re.compile(r"^[^,;<>\s]+@[^,;<>\s]+$")
_CORRESPONDENT_HEADERS = ("from", "to", "cc", "bcc")


@dataclass(frozen=True)
class Correspondent:
    display: str
    email: str
    name: str
    last_seen: int = 0


def _name_from_display(display: str) -> str:
    match = _NAME_FROM_DISPLAY.match(display.strip())
    if match is None:
        return ""
    return match.group(1).strip().strip('"')


def _email_and_name_from_display(display: str) -> tuple[str, str]:
    """Extract lowercase email and display name without Camel (hot harvest path)."""
    display = display.strip().strip(",")
    if not display:
        return "", ""
    match = _ANGLE_EMAIL.search(display)
    if match:
        email = match.group(1).strip().lower()
        name = display[: match.start()].strip().strip('"')
        return email, name
    if _BARE_EMAIL.match(display):
        return display.lower(), ""
    email = normalize_email(display)
    if email and "@" in email:
        return email, _name_from_display(display)
    return "", ""


def _correspondent_from_display(
    display: str, *, last_seen: int = 0
) -> Correspondent | None:
    email, name = _email_and_name_from_display(display)
    if not email or "@" not in email:
        return None
    return Correspondent(
        display=display.strip(),
        email=email,
        name=name,
        last_seen=last_seen,
    )


def folder_feeds_correspondents(folder_name: str | None) -> bool:
    """False for Junk, Drafts, Outbox, and virtual/local helper folders (#313)."""
    if not folder_name:
        return False
    if is_post_outbox_folder(folder_name) or is_post_local_folder(folder_name):
        return False
    if is_virtual_folder(folder_name):
        return False
    if is_junk_folder_name(folder_name):
        return False
    if is_drafts_folder_name([], folder_name):
        return False
    return True


def merge_correspondents(
    existing: list[Correspondent],
    incoming: list[Correspondent],
) -> list[Correspondent]:
    """Dedupe by email, keeping the newest last_seen display string."""
    by_email: dict[str, Correspondent] = {}
    for item in existing:
        by_email[item.email] = item
    for item in incoming:
        previous = by_email.get(item.email)
        if previous is None or item.last_seen > previous.last_seen:
            by_email[item.email] = item
    return sorted(
        by_email.values(),
        key=lambda item: item.last_seen,
        reverse=True,
    )


def collect_correspondents(
    messages: list[dict[str, Any]],
    *,
    exclude_emails: set[str] | None = None,
) -> list[Correspondent]:
    """Collect unique correspondents from message headers.

    Prefers the display string from the newest ``sort_date``. Same-timestamp
    duplicates keep the first-seen display.
    """
    excluded = {email.casefold() for email in (exclude_emails or set()) if email}
    by_email: dict[str, Correspondent] = {}

    for message in messages:
        last_seen = message.get("sort_date") or 0
        if not isinstance(last_seen, int):
            try:
                last_seen = int(last_seen)
            except (TypeError, ValueError):
                last_seen = 0
        for header in _CORRESPONDENT_HEADERS:
            raw = message.get(header) or ""
            if not raw:
                continue
            for display in _ADDRESS_SPLIT.split(raw):
                correspondent = _correspondent_from_display(
                    display, last_seen=last_seen
                )
                if correspondent is None:
                    continue
                if correspondent.email in excluded:
                    continue
                previous = by_email.get(correspondent.email)
                if previous is not None and correspondent.last_seen <= previous.last_seen:
                    continue
                by_email[correspondent.email] = correspondent

    return sorted(
        by_email.values(),
        key=lambda item: item.last_seen,
        reverse=True,
    )


def current_address_token(text: str) -> str:
    """Return the active recipient token after the last comma."""
    text = text or ""
    parts = _ADDRESS_SPLIT.split(text)
    if not parts:
        return text.strip()
    return parts[-1].strip()


def _matches_prefix(value: str, prefix: str) -> bool:
    return value.casefold().startswith(prefix.casefold())


def _email_domain(email: str) -> str:
    at = email.rfind("@")
    if at < 0:
        return ""
    return email[at + 1 :]


def _matches_email_prefix(email: str, prefix: str) -> bool:
    if _matches_prefix(email, prefix):
        return True
    domain = _email_domain(email)
    if not domain:
        return False
    domain_prefix = prefix.lstrip("@")
    return bool(domain_prefix) and _matches_prefix(domain, domain_prefix)


def correspondent_matches_prefix(correspondent: Correspondent, prefix: str) -> bool:
    """Return whether a single correspondent matches a typed prefix."""
    prefix = prefix.strip()
    if not prefix:
        return False
    if correspondent.name and _matches_prefix(correspondent.name, prefix):
        return True
    if _matches_email_prefix(correspondent.email, prefix):
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
        elif _matches_email_prefix(candidate.email, prefix):
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
