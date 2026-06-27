# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dedicated GLib thread for blocking Camel SMTP / IMAP I/O.

Threading contract
------------------
* The mail I/O thread owns a private ``GMainContext`` and runs a continuous
  ``iteration()`` loop so Camel ``*_sync`` calls do not marshal onto the GTK
  main loop.
* UI and other threads must dispatch blocking mail work through
  :func:`get_mail_io_thread` — use :meth:`MailIoThread.submit` for fire-and-forget
  work and :meth:`MailIoThread.run_sync` when the caller can block.
* Never call :meth:`MailIoThread.run_sync` from the GTK main thread (blocks the UI).
  Prefer :meth:`MailIoThread.submit` from GTK, or :func:`run_on_mail_thread` which
  runs inline when already on the mail I/O thread.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import gi

gi.require_version("Camel", "1.2")
gi.require_version("GLib", "2.0")

from gi.repository import Camel, GLib

log = logging.getLogger(__name__)

T = TypeVar("T")

_io_thread_id: int | None = None
_instance: MailIoThread | None = None
_instance_lock = threading.Lock()
_camel_initialized = False


def is_mail_io_thread() -> bool:
    return _io_thread_id is not None and threading.get_ident() == _io_thread_id


def get_mail_io_thread() -> MailIoThread:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MailIoThread()
        return _instance


def run_on_mail_thread(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run blocking mail work on the mail I/O thread.

    Executes ``func`` inline when the caller is already on that thread.
    """
    if is_mail_io_thread():
        return func(*args, **kwargs)
    return get_mail_io_thread().run_sync(func, *args, **kwargs)


def _bootstrap_camel_on_mail_thread() -> None:
    global _camel_initialized
    if _camel_initialized:
        return
    user_data = os.path.expanduser("~/.local/share/evolution")
    Camel.init(user_data, False)
    _camel_initialized = True
    log.debug("Camel.init completed on mail I/O thread")


@dataclass
class _IoTask:
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


class MailIoThread:
    """Serial queue for blocking Camel work off the GTK main loop."""

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
        if is_mail_io_thread():
            return func(*args, **kwargs)
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
        _bootstrap_camel_on_mail_thread()
        self._ready.set()
        log.debug("Mail I/O thread started")

        while True:
            try:
                task = self._queue.get(timeout=0.05)
            except queue.Empty:
                pass
            else:
                func_name = getattr(task.func, "__qualname__", repr(task.func))
                log.debug(
                    "Mail I/O task start thread=%s func=%s",
                    threading.current_thread().name,
                    func_name,
                )
                task_start = GLib.get_monotonic_time()
                try:
                    task.result["value"] = task.func(*task.args, **task.kwargs)
                except BaseException as exc:
                    task.result["error"] = exc
                    log.debug("Mail I/O task failed func=%s", func_name, exc_info=True)
                else:
                    elapsed_ms = (GLib.get_monotonic_time() - task_start) / 1000
                    log.debug(
                        "Mail I/O task finish thread=%s func=%s elapsed=%.1fms",
                        threading.current_thread().name,
                        func_name,
                        elapsed_ms,
                    )
                finally:
                    if task.done is not None:
                        task.done.set()

            while context.pending():
                context.iteration(False)
