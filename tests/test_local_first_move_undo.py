# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local-first move undo: cancel while queued, reverse after flush (#499)."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from post.mail.eds import MailService
from post.mail.operation_queue import (
    QueuedOperation,
    count_queued_operations,
    enqueue_operation,
    list_queued_operations,
)
from post.window import MainWindow


class FlushUndoEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._ops = mock.patch(
            "post.mail.operation_queue.operations_dir",
            return_value=self._tmpdir.name,
        )
        self._ops.start()
        self._pending = mock.patch(
            "post.mail.pending_removals.pending_removals_dir",
            return_value=self._tmpdir.name + "-pending",
        )
        self._pending.start()

    def tearDown(self) -> None:
        self._pending.stop()
        self._ops.stop()
        self._tmpdir.cleanup()

    def test_flush_collects_undo_events_with_dest_uids(self) -> None:
        service = MailService(registry=mock.Mock())
        enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["src-1", "src-2"],
            )
        )
        service._note_pending_local_removals("acct-1", "INBOX", ["src-1", "src-2"])
        with mock.patch.object(
            service,
            "_execute_queued_operation_unlocked",
            return_value={
                "moved_uids": ["src-1", "src-2"],
                "destination_folder": "Archive",
                "destination_uids": ["dst-1", "dst-2"],
            },
        ):
            result = service._flush_operation_queue_unlocked("acct-1")
        events = result.get("undo_events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["destination_folder"], "Archive")
        self.assertEqual(events[0]["destination_uids"], ["dst-1", "dst-2"])
        self.assertEqual(events[0]["moved_uids"], ["src-1", "src-2"])
        self.assertEqual(list_queued_operations(), [])

    def test_normalize_flush_result_includes_undo_events(self) -> None:
        count, failures, events = MailService._normalize_flush_operation_result(
            {
                "flushed": 2,
                "hard_failures": [{"error": "x"}],
                "undo_events": [{"destination_uids": ["d1"]}],
            }
        )
        self.assertEqual(count, 2)
        self.assertEqual(len(failures), 1)
        self.assertEqual(events[0]["destination_uids"], ["d1"])

    def test_flush_defers_when_account_user_offline(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = True
        enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["1"],
            )
        )
        with (
            mock.patch(
                "post.mail.eds.get_account_user_online",
                return_value=False,
            ),
            mock.patch.object(
                service, "_execute_queued_operation_unlocked"
            ) as execute,
        ):
            result = service._flush_operation_queue_unlocked("acct-1")
        execute.assert_not_called()
        self.assertEqual(int(result.get("flushed") or 0), 0)
        self.assertEqual(result.get("hard_failures"), [])
        self.assertEqual(count_queued_operations(), 1)

    def test_background_flush_does_not_reschedule_after_hard_failure(self) -> None:
        service = MailService(registry=mock.Mock())
        service._last_flush_hard_failures = [{"error": "boom"}]
        reschedule_calls = {"n": 0}
        real_schedule = service.schedule_operation_queue_flush

        def _tracking_schedule(*args, **kwargs):
            reschedule_calls["n"] += 1
            return real_schedule(*args, **kwargs)

        def _fake_submit(_name: str, worker) -> None:
            worker()

        service.schedule_operation_queue_flush = _tracking_schedule  # type: ignore[method-assign]
        with (
            mock.patch.object(service, "submit_background", side_effect=_fake_submit),
            mock.patch.object(service, "flush_operation_queue", return_value=0),
            mock.patch(
                "post.mail.eds.count_queued_operations", return_value=1
            ),
        ):
            service.schedule_operation_queue_flush()
        # Initial schedule only — worker must not recurse after hard failure.
        self.assertEqual(reschedule_calls["n"], 1)


class CancelQueuedTransfersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._ops = mock.patch(
            "post.mail.operation_queue.operations_dir",
            return_value=self._tmpdir.name,
        )
        self._ops.start()
        self._pending = mock.patch(
            "post.mail.pending_removals.pending_removals_dir",
            return_value=self._tmpdir.name + "-pending",
        )
        self._pending.start()

    def tearDown(self) -> None:
        self._pending.stop()
        self._ops.stop()
        self._tmpdir.cleanup()

    @mock.patch("post.mail.eds.camel_helpers_enabled", return_value=False)
    def test_cancel_removes_queue_pending_and_restores(self, _helpers_off) -> None:
        service = MailService(registry=mock.Mock())
        enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["a", "b"],
            )
        )
        service._note_pending_local_removals("acct-1", "INBOX", ["a", "b"])
        with mock.patch.object(
            service,
            "_restore_cancelled_transfer_messages_unlocked",
            return_value={
                "messages": [{"uid": "a"}, {"uid": "b"}],
                "source_folder_unread": 1,
                "source_folder_total": 2,
            },
        ) as restore:
            with mock.patch("post.mail.eds.run_on_mail_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)):
                result = service.cancel_queued_transfers(
                    "acct-1", "INBOX", ["a", "b"]
                )
        self.assertEqual(set(result["cancelled_uids"]), {"a", "b"})
        self.assertEqual(result["in_flight_uids"], [])
        self.assertEqual(list_queued_operations(), [])
        restore.assert_called_once()


class WindowMoveUndoArmingTests(unittest.TestCase):
    def _window(self) -> MainWindow:
        win = MainWindow.__new__(MainWindow)
        win._pending_move_undo = None
        win._undo_toast = None
        win._undo_move_action = mock.Mock()
        win._toast_overlay = mock.Mock()
        win._sidebar = mock.Mock()
        win._sidebar.get_move_menu_state.return_value = {
            "archive_folder": "Archive",
            "trash_folder": "Trash",
        }
        win._set_status = mock.Mock()  # type: ignore[method-assign]
        win._refresh_status_display = mock.Mock()  # type: ignore[method-assign]
        win._queued_sync_status = mock.Mock(return_value="queued")  # type: ignore[method-assign]
        return win

    def test_arm_move_undo_allows_source_uids_without_dest(self) -> None:
        win = self._window()
        ok = MainWindow._arm_move_undo(
            win,
            account_uid="acct-1",
            source_folder="INBOX",
            dest_folder="Archive",
            dest_uids=[],
            source_uids=["1", "2"],
            op_type="archive",
        )
        self.assertTrue(ok)
        self.assertEqual(win._pending_move_undo["source_uids"], ["1", "2"])
        self.assertEqual(win._pending_move_undo["dest_uids"], [])
        win._undo_move_action.set_enabled.assert_called_with(True)

    def test_finalize_queued_arms_undo_instead_of_clearing(self) -> None:
        win = self._window()
        MainWindow._finalize_move_status_and_undo(
            win,
            "acct-1",
            "INBOX",
            "archive",
            ["1", "2"],
            {
                "moved_uids": ["1", "2"],
                "queued": True,
                "destination_uids": [],
            },
            status_label="Archived 2 messages",
        )
        self.assertIsNotNone(win._pending_move_undo)
        self.assertEqual(win._pending_move_undo["source_uids"], ["1", "2"])
        self.assertEqual(win._pending_move_undo["op_type"], "archive")
        win._undo_move_action.set_enabled.assert_called_with(True)

    def test_bulk_archive_finished_arms_cancel_undo(self) -> None:
        win = self._window()
        win._bulk_archive_progress_toast = None
        win._suppress_sync_list_reload = ("acct-1", "INBOX")
        win._current_account = mock.Mock(uid="acct-1")
        win._current_folder = "INBOX"
        win._message_list_view = mock.Mock()
        win._message_list_view.item_count.return_value = 0
        win._message_empty_label = mock.Mock()
        win._message_stack = mock.Mock()
        win._update_sidebar_from_move_result = mock.Mock()  # type: ignore[method-assign]
        win._update_message_toolbar = mock.Mock()  # type: ignore[method-assign]
        with mock.patch("post.window.GLib") as glib:
            glib.idle_add = mock.Mock()
            MainWindow._on_sidebar_bulk_archive_finished(
                win,
                "acct-1",
                "INBOX",
                {
                    "archived_count": 2,
                    "moved_uids": ["1", "2"],
                    "destination_uids": [],
                    "queued": True,
                },
                "Archived 2 messages",
            )
        self.assertIsNotNone(win._pending_move_undo)
        self.assertEqual(win._pending_move_undo["source_uids"], ["1", "2"])
        glib.idle_add.assert_called()
        self.assertEqual(
            glib.idle_add.call_args.args[0], win._show_move_undo_toast
        )

    def test_flush_undo_events_upgrade_pending(self) -> None:
        win = self._window()
        MainWindow._arm_move_undo(
            win,
            account_uid="acct-1",
            source_folder="INBOX",
            dest_folder="",
            dest_uids=[],
            source_uids=["1"],
            op_type="archive",
        )
        MainWindow._apply_flush_undo_events(
            win,
            [
                {
                    "account_uid": "acct-1",
                    "source_folder": "INBOX",
                    "moved_uids": ["1"],
                    "destination_folder": "Archive",
                    "destination_uids": ["d1"],
                    "op_type": "archive",
                }
            ],
        )
        self.assertEqual(win._pending_move_undo["dest_folder"], "Archive")
        self.assertEqual(win._pending_move_undo["dest_uids"], ["d1"])

    def test_awaiting_flush_auto_reverses_on_undo_event(self) -> None:
        win = self._window()
        MainWindow._arm_move_undo(
            win,
            account_uid="acct-1",
            source_folder="INBOX",
            dest_folder="",
            dest_uids=[],
            source_uids=["1"],
            op_type="archive",
            awaiting_flush=True,
        )
        win._run_immediate_reverse_undo = mock.Mock()  # type: ignore[method-assign]
        MainWindow._apply_flush_undo_events(
            win,
            [
                {
                    "account_uid": "acct-1",
                    "source_folder": "INBOX",
                    "moved_uids": ["1"],
                    "destination_folder": "Archive",
                    "destination_uids": ["d1"],
                    "op_type": "archive",
                }
            ],
        )
        win._run_immediate_reverse_undo.assert_called_once()
        self.assertIsNone(win._pending_move_undo)


if __name__ == "__main__":
    unittest.main()
