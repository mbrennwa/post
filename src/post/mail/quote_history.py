# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Split message bodies into new content vs quoted history."""

from __future__ import annotations

import re

_QUOTE_HISTORY_REGEXES = (
    re.compile(
        r'\bid\s*=\s*["\']mail-editor-reference-message-container["\']',
        re.IGNORECASE,
    ),
    re.compile(r'\bid\s*=\s*["\']geary-quote["\']', re.IGNORECASE),
    re.compile(
        r'\bclass\s*=\s*["\'][^"\']*\bgmail_quote\b[^"\']*["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r"\bclass\s*=\s*['\"][^'\"]*\bgmail_quote\b[^'\"]*['\"]",
        re.IGNORECASE,
    ),
    re.compile(r'\bclass\s*=\s*["\'][^"\']*\bpost_quote\b[^"\']*["\']', re.IGNORECASE),
    re.compile(
        r"\bclass\s*=\s*['\"][^'\"]*\bpost_quote\b[^'\"]*['\"]",
        re.IGNORECASE,
    ),
    re.compile(r'\bid\s*=\s*["\']appendonsend["\']', re.IGNORECASE),
    re.compile(r'\bid\s*=\s*["\']divrplyfwdmsg["\']', re.IGNORECASE),
    re.compile(r"<blockquote\b", re.IGNORECASE),
)

_PLAIN_QUOTE_CUTS = (
    re.compile(r"(?m)^-----Original Message-----"),
    re.compile(r"(?m)^On .+ wrote:\s*$"),
    re.compile(r"(?m)^>"),
)


def _quote_history_boundary_start(body_html: str, match: re.Match[str]) -> int:
    """Return the index where quoted history begins for a marker match."""
    if match.group(0).lstrip().lower().startswith("<blockquote"):
        return match.start()
    boundary = body_html.rfind("<", 0, match.start())
    if boundary == -1:
        return match.start()
    return boundary


def split_html_at_quote_history(body_html: str) -> tuple[str, str | None]:
    """Split HTML into content before quoted history and the quoted suffix."""
    cut = len(body_html)
    for pattern in _QUOTE_HISTORY_REGEXES:
        match = pattern.search(body_html)
        if match is not None:
            cut = min(cut, _quote_history_boundary_start(body_html, match))
    if cut >= len(body_html):
        return body_html, None
    prefix = body_html[:cut]
    quoted = body_html[cut:]
    if not quoted.strip():
        return body_html, None
    return prefix, quoted


def unquoted_html(body_html: str | None) -> str:
    """Return HTML before quoted history, or empty when *body_html* is missing."""
    if not body_html:
        return ""
    prefix, _quoted = split_html_at_quote_history(body_html)
    return prefix


def unquoted_plain(body_plain: str | None) -> str:
    """Return plain text before quoted history, or empty when missing."""
    if not body_plain:
        return ""
    cut: int | None = None
    for pattern in _PLAIN_QUOTE_CUTS:
        match = pattern.search(body_plain)
        if match is None:
            continue
        start = match.start()
        if cut is None or start < cut:
            cut = start
    if cut is None:
        return body_plain
    return body_plain[:cut].rstrip()
