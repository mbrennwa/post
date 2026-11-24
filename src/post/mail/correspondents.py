# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Correspondent extraction and address autocomplete helpers."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
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


# Ranking weights (#544). Sent evidence (mail written to an address) counts for
# much more than received evidence; counts are log-damped and received counts are
# capped so one chatty sender cannot outrank an address that was written to.
SENT_WEIGHT = 5.0
RECEIVED_WEIGHT = 1.0
RECEIVED_COUNT_CAP = 20
RECENCY_HALF_LIFE_SECONDS = 180 * 24 * 3600
RECENCY_FLOOR = 0.05


@dataclass(frozen=True)
class Correspondent:
    """One address seen in mail headers.

    ``sent_count`` counts messages in Sent folders addressed to the address.
    ``received_count`` counts messages from the address in other folders.
    Appearances that carry no ranking evidence (e.g. Cc recipients of received
    mail, or the sender header of Sent mail) only update ``last_seen`` so the
    address stays suggestible. ``last_seen`` is the newest appearance of any kind.
    """

    display: str
    email: str
    name: str
    last_seen: int = 0
    sent_count: int = 0
    received_count: int = 0
    last_sent: int = 0
    last_received: int = 0


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


def aggregate_correspondents(
    groups: Iterable[Iterable[Correspondent]],
) -> list[Correspondent]:
    """Combine per-folder contributions into one account-wide index.

    Counts are summed and timestamps take the maximum. Display and name come from
    the contribution with the newest ``last_seen`` (first one wins on ties).
    The result is newest-first by ``last_seen``.
    """
    by_email: dict[str, Correspondent] = {}
    for group in groups:
        for item in group:
            previous = by_email.get(item.email)
            if previous is None:
                by_email[item.email] = item
                continue
            newer = item if item.last_seen > previous.last_seen else previous
            by_email[item.email] = Correspondent(
                display=newer.display,
                email=item.email,
                name=newer.name,
                last_seen=max(previous.last_seen, item.last_seen),
                sent_count=previous.sent_count + item.sent_count,
                received_count=previous.received_count + item.received_count,
                last_sent=max(previous.last_sent, item.last_sent),
                last_received=max(previous.last_received, item.last_received),
            )
    return sorted(
        by_email.values(),
        key=lambda item: item.last_seen,
        reverse=True,
    )


class _Accumulator:
    __slots__ = (
        "display",
        "name",
        "last_seen",
        "sent_count",
        "received_count",
        "last_sent",
        "last_received",
    )

    def __init__(self, display: str, name: str, last_seen: int) -> None:
        self.display = display
        self.name = name
        self.last_seen = last_seen
        self.sent_count = 0
        self.received_count = 0
        self.last_sent = 0
        self.last_received = 0


def _message_timestamp(message: dict[str, Any]) -> int:
    value = message.get("sort_date") or 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect_correspondents(
    messages: list[dict[str, Any]],
    *,
    exclude_emails: set[str] | None = None,
    is_sent_folder: bool = False,
) -> list[Correspondent]:
    """Collect unique correspondents with usage counts from message headers.

    Counts are recomputed from ``messages`` on every call, so collecting the
    same folder twice yields the same result (callers replace a folder's
    contribution instead of adding to it).

    In a Sent folder, ``to``/``cc``/``bcc`` count as sent evidence and ``from``
    (normally the user) is presence-only. In other folders ``from`` counts as
    received evidence and ``to``/``cc``/``bcc`` are presence-only, so the user's
    own address does not accumulate a count from every received message.
    An address counts at most once per message and direction.

    Prefers the display string from the newest ``sort_date``. Same-timestamp
    duplicates keep the first-seen display.
    """
    excluded = {email.casefold() for email in (exclude_emails or set()) if email}
    # Mutable accumulators keep the hot harvest path cheap; the frozen
    # Correspondent objects are built once at the end.
    by_email: dict[str, _Accumulator] = {}

    for message in messages:
        timestamp = _message_timestamp(message)
        counted: set[str] = set()
        for header in _CORRESPONDENT_HEADERS:
            raw = message.get(header) or ""
            if not raw:
                continue
            counts_as_sent = is_sent_folder and header != "from"
            counts_as_received = not is_sent_folder and header == "from"
            for display in _ADDRESS_SPLIT.split(raw):
                email, name = _email_and_name_from_display(display)
                if not email or "@" not in email or email in excluded:
                    continue
                acc = by_email.get(email)
                if acc is None:
                    acc = by_email[email] = _Accumulator(
                        display.strip(), name, timestamp
                    )
                elif timestamp > acc.last_seen:
                    acc.display = display.strip()
                    acc.name = name
                    acc.last_seen = timestamp
                if email in counted:
                    continue
                if counts_as_sent:
                    acc.sent_count += 1
                    acc.last_sent = max(acc.last_sent, timestamp)
                    counted.add(email)
                elif counts_as_received:
                    acc.received_count += 1
                    acc.last_received = max(acc.last_received, timestamp)
                    counted.add(email)

    return sorted(
        (
            Correspondent(
                display=acc.display,
                email=email,
                name=acc.name,
                last_seen=acc.last_seen,
                sent_count=acc.sent_count,
                received_count=acc.received_count,
                last_sent=acc.last_sent,
                last_received=acc.last_received,
            )
            for email, acc in by_email.items()
        ),
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


def _recency_factor(timestamp: int, now: float) -> float:
    """Half-life decay in [RECENCY_FLOOR, 1]; future or missing dates are clamped."""
    if timestamp <= 0:
        return RECENCY_FLOOR
    age = max(0.0, now - timestamp)
    return max(RECENCY_FLOOR, 0.5 ** (age / RECENCY_HALF_LIFE_SECONDS))


def correspondent_score(correspondent: Correspondent, now: float) -> float:
    """Recency-weighted frequency score; sent mail outweighs received mail."""
    sent = SENT_WEIGHT * math.log1p(correspondent.sent_count) * _recency_factor(
        correspondent.last_sent, now
    )
    received = (
        RECEIVED_WEIGHT
        * math.log1p(min(correspondent.received_count, RECEIVED_COUNT_CAP))
        * _recency_factor(correspondent.last_received, now)
    )
    return sent + received


def match_correspondents(
    candidates: list[Correspondent],
    prefix: str,
    *,
    limit: int = 10,
    now: float | None = None,
) -> list[Correspondent]:
    """Return prefix matches on name, email, or display string.

    Matches are ranked by :func:`correspondent_score` (frequency weighted by
    recency, sent over received). The match type (name, then email, then
    display) only breaks ties, followed by recency of any appearance (#544).
    """
    prefix = prefix.strip()
    if not prefix:
        return []
    if now is None:
        now = time.time()

    ranked: list[tuple[float, int, float, str, Correspondent]] = []

    for candidate in candidates:
        if candidate.name and _matches_prefix(candidate.name, prefix):
            tier = 0
        elif _matches_email_prefix(candidate.email, prefix):
            tier = 1
        elif _matches_prefix(candidate.display, prefix):
            tier = 2
        else:
            continue
        ranked.append(
            (
                -correspondent_score(candidate, now),
                tier,
                -min(float(candidate.last_seen), now),
                candidate.email,
                candidate,
            )
        )

    ranked.sort(key=lambda entry: entry[:4])
    return [entry[4] for entry in ranked[:limit]]


def apply_address_completion(full_text: str, selected_display: str) -> str:
    """Replace the current token with the selected address."""
    token = current_address_token(full_text)
    if token:
        prefix = full_text[: len(full_text) - len(token)]
    else:
        prefix = full_text
    return f"{prefix}{selected_display}, "
