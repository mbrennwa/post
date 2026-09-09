# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Folder search / list-reader tracing for diagnostics (#120, #375).

Enable terminal diagnostics::

    POST_DEBUG_SEARCH=1 PYTHONPATH=src python3 -m post.main

List/reader UID isolation (#375) also accepts::

    POST_DEBUG_LIST_READER=1 PYTHONPATH=src python3 -m post.main

``POST_LOG_LEVEL=DEBUG`` also enables these messages (search logger is not
filtered below DEBUG).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_logger = logging.getLogger("post.search")
_configured = False


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def search_debug_enabled() -> bool:
    if _env_flag("POST_DEBUG_SEARCH") or _env_flag("POST_DEBUG_LIST_READER"):
        return True
    log_level = os.environ.get("POST_LOG_LEVEL", "").strip().upper()
    return log_level in ("DEBUG", "TRACE")


def list_reader_debug_enabled() -> bool:
    """True when #375 list/reader UID traces should run."""
    if _env_flag("POST_DEBUG_LIST_READER") or _env_flag("POST_DEBUG_SEARCH"):
        return True
    log_level = os.environ.get("POST_LOG_LEVEL", "").strip().upper()
    return log_level in ("DEBUG", "TRACE")


def configure_search_debug_logging() -> None:
    global _configured
    if _configured or not search_debug_enabled():
        return
    _configured = True
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    _logger.setLevel(logging.DEBUG)
    # File/stream handlers default to INFO (#202); lower them so list/reader
    # traces actually reach ~/.local/state/post/post.log.
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in root.handlers:
        if handler.level > logging.DEBUG:
            handler.setLevel(logging.DEBUG)


def search_trace(event: str, /, **fields: Any) -> None:
    """Emit one search diagnostic line when debug tracing is enabled."""
    if not search_debug_enabled():
        return
    configure_search_debug_logging()
    parts = " ".join(f"{key}={value!r}" for key, value in fields.items())
    thread = threading.current_thread().name
    _logger.debug("%s thread=%s %s", event, thread, parts)


def list_reader_trace(event: str, /, **fields: Any) -> None:
    """Emit one list/reader sync diagnostic line (#375)."""
    if not list_reader_debug_enabled():
        return
    configure_search_debug_logging()
    parts = " ".join(f"{key}={value!r}" for key, value in fields.items())
    thread = threading.current_thread().name
    _logger.debug("%s thread=%s %s", event, thread, parts)


class search_trace_timer:
    """Log elapsed milliseconds on exit when debug tracing is enabled."""

    def __init__(self, event: str, /, **fields: Any) -> None:
        self._event = event
        self._fields = fields
        self._start = time.monotonic()

    def __enter__(self) -> search_trace_timer:
        search_trace(f"{self._event}_start", **self._fields)
        return self

    def __exit__(self, exc_type, exc, _tb) -> None:
        elapsed_ms = (time.monotonic() - self._start) * 1000
        if exc is not None:
            search_trace(
                f"{self._event}_error",
                elapsed_ms=round(elapsed_ms, 1),
                error=f"{exc_type.__name__}: {exc}",
                **self._fields,
            )
        else:
            search_trace(
                f"{self._event}_done",
                elapsed_ms=round(elapsed_ms, 1),
                **self._fields,
            )
