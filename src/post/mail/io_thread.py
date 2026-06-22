# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dedicated GLib thread for blocking Camel SMTP / IMAP I/O."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

log = logging.getLogger(__name__)

T = TypeVar("T")

_io_thread_id: int | None = None
_instance: MailIoThread | None = None
_instance_lock = threading.Lock()


def is_mail_io_thread() -> bool:
    return _io_thread_id is not None and threading.get_ident() == _io_thread_id


def get_mail_io_thread() -> MailIoThread:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MailIoThread()
        return _instance


@dataclass
class _IoTask:
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


class MailIoThread:
    """Run blocking Camel work off the GTK main loop."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[_IoTask] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="post-mail-io",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        self._queue.put(_IoTask(func=func, args=args, kwargs=kwargs))

    def run_sync(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        done = threading.Event()
        task = _IoTask(func=func, args=args, kwargs=kwargs, done=done)
        self._queue.put(task)
        done.wait()
        if "error" in task.result:
            raise task.result["error"]
        return task.result.get("value")

    def _thread_main(self) -> None:
        global _io_thread_id
        _io_thread_id = threading.get_ident()
        context = GLib.MainContext.new()
        context.push_thread_default()
        self._ready.set()
        log.debug("Mail I/O thread started")

        while True:
            try:
                task = self._queue.get(timeout=0.05)
            except queue.Empty:
                pass
            else:
                try:
                    task.result["value"] = task.func(*task.args, **task.kwargs)
                except BaseException as exc:
                    task.result["error"] = exc
                    log.debug("Mail I/O task failed", exc_info=True)
                finally:
                    if task.done is not None:
                        task.done.set()

            while context.pending():
                context.iteration(False)
