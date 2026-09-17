# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Epic #422 / #435 lane helpers: same-folder lock + off-Camel Graph HTTP worker.

Today's ``submit`` vs ``submit_background`` is preempt priority on one FIFO.
Epic lanes are **foreground** (folder you're in, open, send) vs **background**
(maintenance). This module supports Phase 2 overlap for M365 background STATUS
via Graph HTTP without a second Camel ``*_sync``.

Gmail concurrent Camel on the same ``MailSession`` stays **fail-closed** unless
``POST_MAIL_GMAIL_CAMEL_OVERLAP=1`` (experimental; default off — Phase 3 input).
"""

from __future__ import annotations

import collections
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

EpicLane = Literal["foreground", "background"]
QueueHint = Literal["interactive", "front", "background"]

_GMAIL_OVERLAP_ENV = "POST_MAIL_GMAIL_CAMEL_OVERLAP"


def gmail_camel_overlap_enabled() -> bool:
    """True only when the experimental same-session Gmail overlap gate is on."""
    raw = (os.environ.get(_GMAIL_OVERLAP_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def default_queue_for_lane(
    lane: EpicLane, *, preemptible: bool | None = None
) -> QueueHint:
    """Map epic lane to today's MailIoThread queue.

    Open-folder refresh is epic **foreground** but stays on the background
    queue when ``preemptible=True`` so interactive clicks can still cancel
    long Graph work (#424 / #435).
    """
    if preemptible is True:
        return "background"
    if preemptible is False:
        return "interactive"
    return "interactive" if lane == "foreground" else "background"


@dataclass
class FolderJobLock:
    """Serialize work per ``(account_uid, folder)``; cross-folder may overlap."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _conds: dict[tuple[str, str], threading.Condition] = field(
        default_factory=dict, init=False, repr=False
    )
    _holders: dict[tuple[str, str], int] = field(
        default_factory=dict, init=False, repr=False
    )

    def _condition(self, key: tuple[str, str]) -> threading.Condition:
        with self._lock:
            cond = self._conds.get(key)
            if cond is None:
                cond = threading.Condition(self._lock)
                self._conds[key] = cond
            return cond

    def run(
        self,
        account_uid: str | None,
        folder: str | None,
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not account_uid or not folder:
            return func(*args, **kwargs)
        key = (account_uid, folder)
        cond = self._condition(key)
        with cond:
            while self._holders.get(key, 0) > 0:
                cond.wait()
            self._holders[key] = self._holders.get(key, 0) + 1
        try:
            return func(*args, **kwargs)
        finally:
            with cond:
                self._holders[key] = max(0, self._holders.get(key, 0) - 1)
                if self._holders[key] == 0:
                    self._holders.pop(key, None)
                cond.notify_all()


class GraphHttpWorker:
    """Serial queue for Graph HTTP that does **not** use ``post-mail-io``."""

    def __init__(self) -> None:
        self._queue: collections.deque[Callable[[], None]] = collections.deque()
        self._work = threading.Condition(threading.Lock())
        self._thread = threading.Thread(
            target=self._thread_main,
            name="post-graph-http",
            daemon=True,
        )
        self._started = False
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._thread.start()
            self._started = True
            log.debug("Graph HTTP worker started")

    def submit(self, func: Callable[[], None]) -> None:
        self._ensure_started()
        with self._work:
            self._queue.append(func)
            self._work.notify()

    def _thread_main(self) -> None:
        while True:
            with self._work:
                while not self._queue:
                    self._work.wait()
                task = self._queue.popleft()
            try:
                task()
            except BaseException:
                log.debug("Graph HTTP task failed", exc_info=True)


_folder_lock = FolderJobLock()
_graph_http_worker = GraphHttpWorker()


def folder_job_lock() -> FolderJobLock:
    return _folder_lock


def graph_http_worker() -> GraphHttpWorker:
    return _graph_http_worker
