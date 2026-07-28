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
* Use :meth:`MailIoThread.submit_background` for long-running offline body download
  and Camel sync-watcher setup; interactive tasks are always scheduled ahead of
  background work.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
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

# Warn when a mail-I/O task runs this long (soft-hang diagnosis, #197).
_LONG_TASK_WARN_SECONDS = 10.0


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


class _TaskPriority(IntEnum):
    INTERACTIVE = 0
    BACKGROUND = 1


@dataclass
class _IoTask:
    func: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    priority: _TaskPriority = _TaskPriority.INTERACTIVE
    done: threading.Event | None = None
    result: dict[str, Any] = field(default_factory=dict)


class MailIoThread:
    """Serial queue for blocking Camel work off the GTK main loop."""

    def __init__(self) -> None:
        self._interactive: collections.deque[_IoTask] = collections.deque()
        self._background: collections.deque[_IoTask] = collections.deque()
        self._lock = threading.Lock()
        self._work_available = threading.Condition(self._lock)
        self._current_is_background = False
        self._current_task_id = 0
        self._background_preempted = False
        self._inside_pump = False
        self._on_background_preempt: Callable[[], None] | None = None
        self._on_background_resume: Callable[[], None] | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="post-mail-io",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def set_background_preempt_callbacks(
        self,
        on_preempt: Callable[[], None] | None,
        on_resume: Callable[[], None] | None,
    ) -> None:
        with self._lock:
            self._on_background_preempt = on_preempt
            self._on_background_resume = on_resume

    def _enqueue_interactive(self, task: _IoTask, *, front: bool = False) -> None:
        preempt: Callable[[], None] | None = None
        with self._work_available:
            # While pump_until drains interactive work cooperatively, do not
            # fire background preempt — that cancels the async op we are
            # pumping (e.g. Archive refresh_info) (#208).
            if (
                self._current_is_background
                and not self._inside_pump
                and self._on_background_preempt is not None
                and not self._background_preempted
            ):
                self._background_preempted = True
                preempt = self._on_background_preempt
            if front:
                self._interactive.appendleft(task)
            else:
                self._interactive.append(task)
            self._work_available.notify()
        if preempt is not None:
            preempt()

    def _maybe_resume_background(self, finished_task: _IoTask) -> None:
        if finished_task.priority is not _TaskPriority.INTERACTIVE:
            return
        resume: Callable[[], None] | None = None
        with self._work_available:
            if (
                self._interactive
                or not self._background_preempted
                or self._on_background_resume is None
            ):
                return
            self._background_preempted = False
            resume = self._on_background_resume
        if resume is not None:
            resume()

    def submit(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        self._enqueue_interactive(_IoTask(func=func, args=args, kwargs=kwargs))

    def submit_front(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Queue interactive work ahead of other pending interactive tasks."""
        self._enqueue_interactive(_IoTask(func=func, args=args, kwargs=kwargs), front=True)

    def submit_background(
        self, func: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> None:
        with self._work_available:
            self._background.append(
                _IoTask(
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    priority=_TaskPriority.BACKGROUND,
                )
            )
            self._work_available.notify()

    def has_interactive_work_pending(self) -> bool:
        with self._lock:
            return bool(self._interactive)

    def pump_until(
        self,
        done: threading.Event,
        *,
        timeout_seconds: float | None = None,
        run_interactive: bool = True,
        on_timeout_cancel: Callable[[], None] | None = None,
    ) -> bool:
        """Pump the mail-thread GLib context until ``done`` is set (#208).

        Must run on the mail I/O thread (e.g. inside a background task waiting on
        Camel async ``refresh_info``). When ``run_interactive`` is True, pending
        interactive tasks are drained between iterations so opening a message or
        switching folders is not frozen behind Graph.
        """
        if not is_mail_io_thread():
            raise RuntimeError("pump_until must run on the mail I/O thread")
        context = GLib.MainContext.get_thread_default()
        if context is None:
            raise RuntimeError("mail I/O thread has no thread-default GMainContext")
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + max(0.0, timeout_seconds)
        )
        timed_out = False
        with self._work_available:
            self._inside_pump = True
        try:
            while not done.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    if on_timeout_cancel is not None:
                        on_timeout_cancel()
                    break
                if run_interactive:
                    nested: _IoTask | None = None
                    with self._work_available:
                        if self._interactive:
                            nested = self._interactive.popleft()
                    if nested is not None:
                        self._run_task_body(nested, nested_under_pump=True)
                        continue
                if context.pending():
                    context.iteration(False)
                else:
                    with self._work_available:
                        if done.is_set():
                            break
                        if run_interactive and self._interactive:
                            continue
                        self._work_available.wait(timeout=0.05)
            if timed_out and not done.is_set():
                # Brief grace so cancel can deliver the async finish callback.
                grace_deadline = time.monotonic() + 2.0
                while not done.is_set() and time.monotonic() < grace_deadline:
                    if context.pending():
                        context.iteration(False)
                    else:
                        time.sleep(0.02)
            return done.is_set()
        finally:
            with self._work_available:
                self._inside_pump = False

    def _run_task_body(
        self, task: _IoTask, *, nested_under_pump: bool = False
    ) -> None:
        func_name = getattr(task.func, "__qualname__", repr(task.func))
        try:
            task.result["value"] = task.func(*task.args, **task.kwargs)
        except BaseException as exc:
            task.result["error"] = exc
            log.debug("Mail I/O task failed func=%s", func_name, exc_info=True)
        finally:
            if task.done is not None:
                task.done.set()
            if not nested_under_pump:
                self._maybe_resume_background(task)

    def run_sync(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        if is_mail_io_thread():
            return func(*args, **kwargs)
        done = threading.Event()
        task = _IoTask(func=func, args=args, kwargs=kwargs, done=done)
        self._enqueue_interactive(task)
        done.wait()
        if "error" in task.result:
            raise task.result["error"]
        return task.result.get("value")

    def _take_next_task(self) -> _IoTask | None:
        if self._interactive:
            return self._interactive.popleft()
        if self._background:
            return self._background.popleft()
        return None

    def _thread_main(self) -> None:
        global _io_thread_id
        _io_thread_id = threading.get_ident()
        context = GLib.MainContext.new()
        context.push_thread_default()
        _bootstrap_camel_on_mail_thread()
        self._ready.set()
        log.debug("Mail I/O thread started")

        while True:
            task: _IoTask | None = None
            with self._work_available:
                while task is None:
                    task = self._take_next_task()
                    if task is not None:
                        break
                    self._work_available.wait(timeout=0.05)

            if task is None:
                while context.pending():
                    context.iteration(False)
                continue

            func_name = getattr(task.func, "__qualname__", repr(task.func))
            log.debug(
                "Mail I/O task start thread=%s func=%s priority=%s",
                threading.current_thread().name,
                func_name,
                task.priority.name,
            )
            with self._work_available:
                self._current_is_background = task.priority is _TaskPriority.BACKGROUND
                self._current_task_id += 1
                current_task_id = self._current_task_id
            task_start = GLib.get_monotonic_time()
            warn_seconds = _LONG_TASK_WARN_SECONDS
            thread_name = threading.current_thread().name

            def _warn_still_running(task_id: int = current_task_id) -> None:
                with self._work_available:
                    if self._current_task_id != task_id:
                        return
                elapsed_ms = (GLib.get_monotonic_time() - task_start) / 1000
                log.warning(
                    "Mail I/O task still running thread=%s func=%s priority=%s "
                    "elapsed=%.1fms",
                    thread_name,
                    func_name,
                    task.priority.name,
                    elapsed_ms,
                )

            watchdog: threading.Timer | None = None
            if warn_seconds > 0:
                watchdog = threading.Timer(warn_seconds, _warn_still_running)
                watchdog.daemon = True
                watchdog.start()
            try:
                task.result["value"] = task.func(*task.args, **task.kwargs)
            except BaseException as exc:
                task.result["error"] = exc
                log.debug("Mail I/O task failed func=%s", func_name, exc_info=True)
                elapsed_ms = (GLib.get_monotonic_time() - task_start) / 1000
                if warn_seconds > 0 and elapsed_ms >= warn_seconds * 1000:
                    log.warning(
                        "Mail I/O task failed slowly thread=%s func=%s "
                        "elapsed=%.1fms",
                        thread_name,
                        func_name,
                        elapsed_ms,
                    )
            else:
                elapsed_ms = (GLib.get_monotonic_time() - task_start) / 1000
                log.debug(
                    "Mail I/O task finish thread=%s func=%s elapsed=%.1fms",
                    thread_name,
                    func_name,
                    elapsed_ms,
                )
                if warn_seconds > 0 and elapsed_ms >= warn_seconds * 1000:
                    log.warning(
                        "Mail I/O task slow finish thread=%s func=%s elapsed=%.1fms",
                        thread_name,
                        func_name,
                        elapsed_ms,
                    )
            finally:
                if watchdog is not None:
                    watchdog.cancel()
                with self._work_available:
                    self._current_is_background = False
                if task.done is not None:
                    task.done.set()
                self._maybe_resume_background(task)

            while context.pending():
                context.iteration(False)
