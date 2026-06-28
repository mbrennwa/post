# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Trace search cancel and message-read scheduling. Enable with POST_DEBUG_SEARCH_CANCEL=1."""

from __future__ import annotations

import logging
import os
import threading

_ENABLED = os.environ.get("POST_DEBUG_SEARCH_CANCEL", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

log = logging.getLogger("post.search-cancel")


def enabled() -> bool:
    return _ENABLED


def configure() -> None:
    if not _ENABLED or log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s search-cancel %(message)s")
    )
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.debug("tracing enabled (POST_DEBUG_SEARCH_CANCEL=1)")


def trace(event: str, /, **fields: object) -> None:
    if not _ENABLED:
        return
    detail = " ".join(f"{key}={value!r}" for key, value in fields.items())
    log.debug("%s thread=%s %s", event, threading.current_thread().name, detail)
