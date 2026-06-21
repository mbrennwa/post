# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python helpers for Camel API values."""

from __future__ import annotations

from typing import Any


def normalize_camel_uid(value: Any) -> str | None:
    """Return a non-zero numeric Camel/IMAP UID string, or None if invalid."""
    uid = str(value).strip()
    if not uid or not uid.isdigit() or uid == "0":
        return None
    return uid


def camel_uid_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        length = value.get_length()
        return [str(value.get_nth(index)) for index in range(length)]
    except (AttributeError, TypeError):
        pass
    try:
        return [str(uid) for uid in value]
    except TypeError:
        return [str(value)]
