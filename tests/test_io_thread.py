# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import threading
import unittest

from post.mail.io_thread import get_mail_io_thread, is_mail_io_thread, run_on_mail_thread


class MailIoThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._io = get_mail_io_thread()

    def test_is_not_mail_io_thread_on_caller(self) -> None:
        self.assertFalse(is_mail_io_thread())

    def test_submit_runs_on_mail_io_thread(self) -> None:
        seen: list[bool] = []

        def worker() -> None:
            seen.append(is_mail_io_thread())

        self._io.submit(worker)
        self._io.run_sync(lambda: None)
        self.assertEqual(seen, [True])

    def test_run_sync_returns_value(self) -> None:
        value = self._io.run_sync(lambda: "ok")
        self.assertEqual(value, "ok")

    def test_run_sync_propagates_exception(self) -> None:
        def boom() -> None:
            raise ValueError("mail task failed")

        with self.assertRaisesRegex(ValueError, "mail task failed"):
            self._io.run_sync(boom)

    def test_submit_preserves_fifo_order(self) -> None:
        results: list[int] = []

        for index in range(5):

            def task(i: int = index) -> None:
                results.append(i)

            self._io.submit(task)

        self._io.run_sync(lambda: None)
        self.assertEqual(results, [0, 1, 2, 3, 4])

    def test_run_sync_tasks_run_in_order(self) -> None:
        results: list[int] = []

        for index in range(3):
            self._io.run_sync(results.append, index)

        self.assertEqual(results, [0, 1, 2])

    def test_run_sync_blocks_until_prior_submit_finishes(self) -> None:
        gate = threading.Event()
        started = threading.Event()

        def slow_task() -> None:
            started.set()
            gate.wait(timeout=5.0)

        self._io.submit(slow_task)
        self.assertTrue(started.wait(timeout=5.0))

        sync_done = threading.Event()

        def waiter() -> None:
            self._io.run_sync(lambda: None)
            sync_done.set()

        threading.Thread(target=waiter, daemon=True).start()
        self.assertFalse(sync_done.wait(timeout=0.2))

        gate.set()
        self.assertTrue(sync_done.wait(timeout=5.0))

    def test_run_sync_on_mail_thread_runs_inline(self) -> None:
        def inner() -> bool:
            return self._io.run_sync(is_mail_io_thread)

        self.assertTrue(self._io.run_sync(inner))

    def test_run_on_mail_thread_executes_on_mail_thread(self) -> None:
        self.assertTrue(run_on_mail_thread(is_mail_io_thread))

    def test_run_on_mail_thread_is_reentrant(self) -> None:
        def nested() -> int:
            return run_on_mail_thread(lambda: 7)

        self.assertEqual(run_on_mail_thread(nested), 7)

    def test_interactive_runs_before_pending_background(self) -> None:
        results: list[str] = []

        self._io.submit_background(lambda: results.append("background"))
        self._io.submit(lambda: results.append("interactive"))
        self._io.run_sync(lambda: None)
        self.assertEqual(results, ["interactive", "background"])

    def test_has_interactive_work_pending(self) -> None:
        gate = threading.Event()
        started = threading.Event()

        def background() -> None:
            started.set()
            gate.wait(timeout=5.0)

        self._io.submit_background(background)
        self.assertTrue(started.wait(timeout=5.0))
        self.assertFalse(self._io.has_interactive_work_pending())
        self._io.submit(lambda: None)
        self.assertTrue(self._io.has_interactive_work_pending())
        gate.set()
        self._io.run_sync(lambda: None)
        self.assertFalse(self._io.has_interactive_work_pending())

    def test_interactive_preempts_running_background(self) -> None:
        gate = threading.Event()
        started = threading.Event()
        preempted = threading.Event()
        results: list[str] = []

        def background() -> None:
            started.set()
            gate.wait(timeout=5.0)
            results.append("background")

        def interactive() -> None:
            results.append("interactive")

        def on_preempt() -> None:
            preempted.set()
            gate.set()

        self._io.set_background_preempt_callbacks(on_preempt, None)
        self._io.submit_background(background)
        self.assertTrue(started.wait(timeout=5.0))
        self._io.submit(interactive)
        self.assertTrue(preempted.wait(timeout=5.0))
        self._io.run_sync(lambda: None)
        self.assertEqual(results, ["background", "interactive"])
