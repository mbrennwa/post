# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import threading
import unittest

from post.mail.io_thread import get_mail_io_thread, is_mail_io_thread


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
