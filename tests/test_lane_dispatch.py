# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Epic lane helpers and folder lock (#435)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from post.mail.eds import MailService
from post.mail.lane_dispatch import (
    FolderJobLock,
    default_queue_for_lane,
    gmail_camel_overlap_enabled,
)


class LaneDispatchTests(unittest.TestCase):
    def test_default_queue_maps_epic_lanes(self) -> None:
        self.assertEqual(default_queue_for_lane("foreground"), "interactive")
        self.assertEqual(default_queue_for_lane("background"), "background")
        self.assertEqual(
            default_queue_for_lane("foreground", preemptible=True),
            "background",
        )

    def test_gmail_overlap_gate_defaults_off(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            # Ensure unset
            import os

            os.environ.pop("POST_MAIL_GMAIL_CAMEL_OVERLAP", None)
            self.assertFalse(gmail_camel_overlap_enabled())

    def test_folder_lock_serializes_same_folder(self) -> None:
        lock = FolderJobLock()
        order: list[str] = []
        started = threading.Event()
        release_first = threading.Event()

        def first() -> None:
            order.append("first-enter")
            started.set()
            release_first.wait(timeout=2.0)
            order.append("first-leave")

        def second() -> None:
            order.append("second-enter")
            order.append("second-leave")

        t1 = threading.Thread(
            target=lambda: lock.run("a", "INBOX", first), daemon=True
        )
        t2 = threading.Thread(
            target=lambda: lock.run("a", "INBOX", second), daemon=True
        )
        t1.start()
        self.assertTrue(started.wait(timeout=2.0))
        t2.start()
        # Second must not enter while first holds the lock.
        self.assertNotIn("second-enter", order)
        release_first.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        self.assertEqual(
            order,
            ["first-enter", "first-leave", "second-enter", "second-leave"],
        )

    def test_folder_lock_allows_cross_folder(self) -> None:
        lock = FolderJobLock()
        both_running = threading.Event()
        hold = threading.Event()
        in_a = threading.Event()
        in_b = threading.Event()

        def work_a() -> None:
            in_a.set()
            if in_b.wait(timeout=2.0):
                both_running.set()
            hold.wait(timeout=2.0)

        def work_b() -> None:
            in_b.set()
            if in_a.wait(timeout=2.0):
                both_running.set()
            hold.wait(timeout=2.0)

        t1 = threading.Thread(
            target=lambda: lock.run("a", "INBOX", work_a), daemon=True
        )
        t2 = threading.Thread(
            target=lambda: lock.run("a", "Archive", work_b), daemon=True
        )
        t1.start()
        t2.start()
        self.assertTrue(both_running.wait(timeout=2.0))
        hold.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)


class MailSubmitJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())

    def test_submit_job_m365_background_counts_use_graph_http(self) -> None:
        ran = threading.Event()

        def worker() -> None:
            ran.set()

        with (
            mock.patch.object(
                self.service, "_account_backend_is_microsoft365", return_value=True
            ),
            mock.patch("post.mail.eds.graph_http_worker") as graph_worker,
            mock.patch.object(self.service, "submit_background") as submit_bg,
        ):
            graph_worker.return_value.submit.side_effect = lambda fn: fn()
            self.service.submit_job(
                "poll_account_folder_counts",
                worker,
                epic_lane="background",
                account_uid="acct-m365",
            )
        graph_worker.return_value.submit.assert_called_once()
        submit_bg.assert_not_called()
        self.assertTrue(ran.is_set())

    def test_submit_job_non_m365_background_uses_helper_worker(self) -> None:
        with (
            mock.patch.object(
                self.service, "_account_backend_is_microsoft365", return_value=False
            ),
            mock.patch.object(
                self.service._camel_pool, "submit_worker"
            ) as submit_helper,
            mock.patch.object(self.service, "submit_background") as submit_bg,
            mock.patch("post.mail.eds.graph_http_worker") as graph_worker,
        ):
            self.service.submit_job(
                "poll_account_folder_counts",
                lambda: None,
                epic_lane="background",
                account_uid="acct-gmail",
            )
        submit_helper.assert_called_once()
        submit_bg.assert_not_called()
        graph_worker.return_value.submit.assert_not_called()

    def test_open_folder_refresh_uses_helper_worker(self) -> None:
        with (
            mock.patch.object(
                self.service._camel_pool, "submit_worker"
            ) as submit_helper,
            mock.patch.object(self.service, "submit_background") as submit_bg,
        ):
            self.service.submit_job(
                "folder_message_sync",
                lambda: None,
                epic_lane="foreground",
                account_uid="acct",
                folder="INBOX",
                preemptible=True,
            )
        submit_helper.assert_called_once()
        submit_bg.assert_not_called()
