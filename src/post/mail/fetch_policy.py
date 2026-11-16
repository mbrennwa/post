# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Size-aware limits for fetching message bodies from the server (#537).

A message body is fetched in one piece and Camel discards a fetch that is
cancelled part-way, so a fixed wall-clock timeout can never succeed for a
message whose transfer simply takes longer than the timeout. The limits here
scale with the message size instead.
"""

from __future__ import annotations

# Interactive read: base allowance for connect / server latency.
MESSAGE_READ_BASE_SECONDS = 30.0
# Lowest sustained throughput we still wait for (bytes per second). Slower
# links hit the ceiling; 128 KiB/s is about 1 Mbit/s.
MESSAGE_READ_MIN_THROUGHPUT_BPS = 128 * 1024
# Never wait longer than this for one message (a dead connection must not pin
# the account's helper for hours).
MESSAGE_READ_MAX_SECONDS = 900.0

# Background work (full-account body download, arrival prefetch) skips
# messages above this size: the user fetches those by opening them. Keeps a
# huge attachment from pinning the account's single Camel helper (#537).
BACKGROUND_FETCH_MAX_BYTES = 10 * 1024 * 1024
# Camel search ``(get-size)`` is in kilobytes.
BACKGROUND_FETCH_MAX_KB = BACKGROUND_FETCH_MAX_BYTES // 1024

# Shown in the reader only when the transfer is long enough to notice.
DOWNLOAD_NOTE_MIN_BYTES = 2 * 1024 * 1024


def fetch_timeout_seconds(
    size_bytes: int | None,
    *,
    base_seconds: float = MESSAGE_READ_BASE_SECONDS,
    min_throughput_bps: int = MESSAGE_READ_MIN_THROUGHPUT_BPS,
    max_seconds: float = MESSAGE_READ_MAX_SECONDS,
) -> float:
    """Return how long to wait for a fetch of *size_bytes*.

    Unknown or non-positive sizes get *base_seconds* so behaviour for ordinary
    messages is unchanged.
    """
    if not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return float(base_seconds)
    allowance = base_seconds + float(size_bytes) / float(min_throughput_bps)
    return float(min(max(allowance, base_seconds), max_seconds))


def background_fetch_allowed(size_bytes: int | None) -> bool:
    """True when a background job may download a body of *size_bytes*."""
    if not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return True
    return size_bytes <= BACKGROUND_FETCH_MAX_BYTES


def format_size(size_bytes: int | float | None) -> str:
    """Short human size ("33.8 MB") for status text."""
    if not isinstance(size_bytes, (int, float)) or size_bytes <= 0:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return ""


def download_note(size_bytes: int | float | None) -> str | None:
    """Reader loading text for a long download, or None for ordinary messages."""
    if (
        not isinstance(size_bytes, (int, float))
        or size_bytes < DOWNLOAD_NOTE_MIN_BYTES
    ):
        return None
    return f"Downloading {format_size(size_bytes)}…"
