# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Parse mailto: URIs (RFC 6068) into compose fields."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlparse


@dataclass(frozen=True)
class MailtoCompose:
    """Fields for opening a new compose window from a mailto: URI."""

    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str = ""
    body: str = ""


def _split_addresses(value: str) -> list[str]:
    """Split a comma-separated address list, preserving quoted commas."""
    text = value.strip()
    if not text:
        return []
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(ch)
    part = "".join(buf).strip()
    if part:
        parts.append(part)
    return parts


def parse_mailto_uri(uri: str) -> MailtoCompose:
    """Parse a mailto: URI into compose fields.

    Raises ValueError when *uri* is not a mailto URI.
    """
    raw = (uri or "").strip()
    if not raw.lower().startswith("mailto:"):
        raise ValueError("Not a mailto: URI")

    parsed = urlparse(raw)
    # urlparse keeps the scheme; path holds addr-spec (may be empty).
    # Some URIs use mailto:?to=... with an empty path.
    path_addrs = _split_addresses(unquote(parsed.path or ""))

    to: list[str] = list(path_addrs)
    cc: list[str] = []
    bcc: list[str] = []
    subject = ""
    body = ""

    # keep_blank_values so body= is distinguished from a missing body.
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        name = unquote(key).strip().lower()
        decoded = unquote(value)
        if name == "to":
            to.extend(_split_addresses(decoded))
        elif name == "cc":
            cc.extend(_split_addresses(decoded))
        elif name == "bcc":
            bcc.extend(_split_addresses(decoded))
        elif name == "subject":
            subject = decoded
        elif name == "body":
            body = decoded.replace("\r\n", "\n").replace("\r", "\n")

    def _dedupe(addrs: list[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        out: list[str] = []
        for addr in addrs:
            key = addr.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(addr)
        return tuple(out)

    return MailtoCompose(
        to=_dedupe(to),
        cc=_dedupe(cc),
        bcc=_dedupe(bcc),
        subject=subject,
        body=body,
    )
