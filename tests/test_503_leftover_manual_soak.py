# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Automated stand-in for leftover #503 manual checks."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from post.mail import pending_removals
from post.mail.eds import MailService
from post.mail.operation_queue import (
    QueuedOperation,
    acquire_flush_lease,
    coalesce_or_enqueue_operation,
    count_queued_operations,
    format_queued_operation_status,
    list_flush_leased_ids,
    list_queued_operations,
    release_all_flush_leases,
)
from post.window import MainWindow


class QueuedStatusManualChecks(unittest.TestCase):
    """Phase 1 / #491 — status names action + real blocker."""

    def setUp(self) -> None:
        self._ops = tempfile.TemporaryDirectory()
        self._ops_patch = mock.patch(
            "post.mail.operation_queue.operations_dir",
            return_value=self._ops.name,
        )
        self._ops_patch.start()
        self.win = MainWindow.__new__(MainWindow)
        self.win._network_available = True
        self.win._current_folder = "INBOX"
        self.win._status_hint = ""
        self.win._mail = mock.Mock()
        self.win._mail.count_queued_operations.return_value = 0
        self.win._mail.get_account_connect_health.return_value = "ok"
        self.win._mail.get_account_transfer_state.return_value = "idle"
        self.win._sidebar = mock.Mock()
        self.win._sidebar.account_display_label.return_value = "Work"

    def tearDown(self) -> None:
        self._ops_patch.stop()
        self._ops.cleanup()

    def test_blocker_never_says_when_online_if_health_ok(self) -> None:
        with mock.patch(
            "post.preferences.get_account_user_online", return_value=True
        ):
            blocker = MainWindow._queued_operation_blocker(self.win, "acct-1")
        self.assertEqual(blocker, "syncing with mail server")
        self.assertNotIn("when online", blocker)

    def test_blocker_sign_in_and_account_offline(self) -> None:
        self.win._mail.get_account_connect_health.return_value = "needs_sign_in"
        with mock.patch(
            "post.preferences.get_account_user_online", return_value=True
        ):
            self.assertEqual(
                MainWindow._queued_operation_blocker(self.win, "acct-1"),
                "will sync after you sign in",
            )
        self.win._mail.get_account_connect_health.return_value = "ok"
        with mock.patch(
            "post.preferences.get_account_user_online", return_value=False
        ):
            self.assertEqual(
                MainWindow._queued_operation_blocker(self.win, "acct-1"),
                "will sync when that account is online",
            )
        self.win._network_available = False
        self.assertEqual(
            MainWindow._queued_operation_blocker(self.win, "acct-1"),
            "will sync when online",
        )

    def test_status_uses_queue_head_not_generic_count(self) -> None:
        coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="move_to_trash",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["1", "2", "3"],
            )
        )
        with mock.patch(
            "post.preferences.get_account_user_online", return_value=True
        ):
            text = MainWindow._queued_sync_status(
                self.win, "acct-1", 1, noun="message"
            )
        self.assertIn("Trash 3 in Work/INBOX", text)
        self.assertIn("syncing with mail server", text)
        self.assertNotIn("will sync when online", text)

    def test_stale_queued_hint_clears_when_queue_empty(self) -> None:
        self.win._status_hint = "Queued: Archive in Work/INBOX — syncing with mail server"
        self.win._mail.count_queued_operations.return_value = 0
        MainWindow._clear_stale_queued_status_hint(self.win)
        self.assertEqual(self.win._status_hint, "")


class HardFlushFailManualChecks(unittest.TestCase):
    """Phase 2 — hard fail clears pending, keeps op, UI reloads."""

    def setUp(self) -> None:
        self._pending = tempfile.TemporaryDirectory()
        self._ops = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch(
                "post.mail.pending_removals.pending_removals_dir",
                return_value=self._pending.name,
            ),
            mock.patch(
                "post.mail.operation_queue.operations_dir",
                return_value=self._ops.name,
            ),
        ]
        for patcher in self._patches:
            patcher.start()
        self.service = MailService(registry=mock.Mock())
        self.service._flushing_queue_ids = set()
        self.service.get_account_connect_health = (  # type: ignore[method-assign]
            lambda _uid: "ok"
        )

    def tearDown(self) -> None:
        for patcher in self._patches:
            patcher.stop()
        self._pending.cleanup()
        self._ops.cleanup()

    def test_hard_fail_keeps_op_clears_pending(self) -> None:
        coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["x"],
            )
        )
        self.service._note_pending_local_removals("acct-1", "INBOX", ["x"])
        with mock.patch.object(
            self.service,
            "_execute_queued_operation_unlocked",
            side_effect=RuntimeError("server refused"),
        ):
            result = self.service._flush_operation_queue_unlocked("acct-1")
        self.assertEqual(int(result.get("flushed") or 0), 0)
        failures = result.get("hard_failures") or []
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["folder_name"], "INBOX")
        self.assertEqual(count_queued_operations(), 1)
        self.assertEqual(pending_removals.load_pending_uids("acct-1", "INBOX"), set())

    def test_ui_hard_fail_callback_reloads_folder(self) -> None:
        win = MainWindow.__new__(MainWindow)
        win._suppress_sync_list_reload = ("acct-1", "INBOX")
        win._status_hint = "Queued: Archive in Work/INBOX — syncing with mail server"
        win._pending_move_undo = None
        win._mail = mock.Mock()
        win._mail.count_queued_operations.return_value = 1
        win._status = mock.Mock()
        loaded: list[tuple[str, str, bool]] = []

        def _load(account_uid: str, folder_name: str, sync: bool = True) -> None:
            loaded.append((account_uid, folder_name, sync))

        win._load_messages = _load  # type: ignore[method-assign]
        win._refresh_status_display = mock.Mock()  # type: ignore[method-assign]
        win._clear_stale_queued_status_hint = mock.Mock()  # type: ignore[method-assign]
        with mock.patch("post.window.show_error_toast") as toast:
            MainWindow._on_operation_queue_flushed(
                win,
                0,
                [
                    {
                        "account_uid": "acct-1",
                        "folder_name": "INBOX",
                        "error": "server refused",
                    }
                ],
            )
        self.assertIsNone(win._suppress_sync_list_reload)
        self.assertEqual(loaded, [("acct-1", "INBOX", False)])
        toast.assert_called_once()


class HelperMutationManualChecks(unittest.TestCase):
    """Phase 3 — draft/empty/delete go through helper IPC."""

    @mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True)
    def test_mutations_route_to_helper(self, _helpers_on) -> None:
        service = MailService(registry=mock.Mock())
        with mock.patch.object(
            service,
            "_camel_helper_call",
            side_effect=[("Drafts", "1"), {"removed_count": 1}, None],
        ) as helper:
            self.assertEqual(
                service.save_draft("acct-1", subject="Hi", body="Body"),
                ("Drafts", "1"),
            )
            self.assertEqual(
                service.empty_folder("acct-1", "Trash")["removed_count"], 1
            )
            service.delete_folder("acct-1", "Old")
        methods = [call.args[0] for call in helper.call_args_list]
        self.assertEqual(methods, ["save_draft", "empty_folder", "delete_folder"])


class HelperKillResumeManualChecks(unittest.TestCase):
    """Phase 4 — kill leaves ops; lease clear allows resume."""

    def setUp(self) -> None:
        self._ops = tempfile.TemporaryDirectory()
        self._ops_patch = mock.patch(
            "post.mail.operation_queue.operations_dir",
            return_value=self._ops.name,
        )
        self._ops_patch.start()

    def tearDown(self) -> None:
        self._ops_patch.stop()
        self._ops.cleanup()

    def test_helper_died_clears_leases_and_reschedules_flush(self) -> None:
        queue_id = coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-kill",
                folder_name="INBOX",
                message_uids=["1"],
            )
        )
        acquire_flush_lease(queue_id)
        self.assertEqual(list_flush_leased_ids(), {queue_id})

        scheduled: list[str] = []

        def _on_helper_died(uid: str) -> None:
            release_all_flush_leases()
            scheduled.append(uid)

        # Mirror MailService.connect wiring (#503 Phase 4).
        _on_helper_died("acct-kill")

        self.assertEqual(list_flush_leased_ids(), set())
        self.assertEqual(list_queued_operations()[0][0], queue_id)
        self.assertEqual(count_queued_operations(), 1)
        self.assertEqual(scheduled, ["acct-kill"])
        # After lease drop, new UIDs may coalesce into the surviving op.
        second = coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-kill",
                folder_name="INBOX",
                message_uids=["2"],
            )
        )
        self.assertEqual(second, queue_id)

    def test_watchdog_kill_fires_died_and_clears_lease(self) -> None:
        """Timeout kill must notify died (reader alone skips after _proc clear)."""
        from post.mail.camel_runtime import AccountCamelRuntime, CamelHelperTimeout

        queue_id = coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="soak-acct",
                folder_name="INBOX",
                message_uids=["u1", "u2"],
            )
        )
        acquire_flush_lease(queue_id)
        events: list[str] = []

        def on_died(uid: str) -> None:
            release_all_flush_leases()
            events.append(uid)

        runtime = AccountCamelRuntime(account_uid="soak-acct")
        runtime.set_died_callback(on_died)
        try:
            with self.assertRaises(CamelHelperTimeout):
                runtime.call("sleep_for_test", [8.0], timeout=0.4)
            self.assertEqual(events, ["soak-acct"])
            self.assertEqual(list_flush_leased_ids(), set())
            self.assertEqual(count_queued_operations(), 1)
            self.assertEqual(list_queued_operations()[0][0], queue_id)
            # Respawn answers again.
            ping = runtime.call("ping", ["soak-acct"], timeout=15.0)
            self.assertTrue(ping.get("ok"))
        finally:
            runtime.kill()

    def test_format_status_acceptance_string(self) -> None:
        text = format_queued_operation_status(
            account_label="Work",
            op_type="archive",
            folder_name="Inbox",
            message_count=2,
            blocker="waiting on mail server",
        )
        self.assertEqual(
            text,
            "Queued: Archive 2 in Work/Inbox — waiting on mail server",
        )


if __name__ == "__main__":
    unittest.main()
