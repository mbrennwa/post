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
        win._reconcile_open_folder_list = mock.Mock()  # type: ignore[method-assign]
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
        win._reconcile_open_folder_list.assert_called_once_with("acct-1", "INBOX")
        win._message_list_view.remove_uids.assert_not_called()
        self.assertEqual(win._suppress_sync_list_reload, ("acct-1", "INBOX"))

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


class OpenFolderReconcileTests(unittest.TestCase):
    def _window(self) -> MainWindow:
        win = MainWindow.__new__(MainWindow)
        win._sidebar = mock.Mock()
        win._sidebar.inbox_folder_for_account.return_value = "INBOX"
        win._sidebar.folder_is_drafts.return_value = False
        win._search_query = None
        win._message_sync_in_progress = False
        win._message_list_populating = False
        win._folder_reconcile_inflight = False
        win._folder_reconcile_again = None
        win._messages_load_generation = 1
        win._message_list_bound_count = 0
        win._pending_flag_changes = {}
        win._reader_windows = []
        win._current_message = None
        win._current_message_uid = None
        win._pending_restore_message_uid = None
        win._restore_message_folder = None
        win._message_list_view = mock.Mock()
        win._message_list_view.get_message.return_value = None
        win._message_empty_label = mock.Mock()
        win._message_stack = mock.Mock()
        win._update_message_toolbar = mock.Mock()  # type: ignore[method-assign]
        win._update_message_status = mock.Mock()  # type: ignore[method-assign]
        win._apply_messages_to_list = mock.Mock()  # type: ignore[method-assign]
        win._clear_reader = mock.Mock()  # type: ignore[method-assign]
        return win

    def _viewing_inbox(self, win: MainWindow) -> None:
        win._current_account = mock.Mock(uid="acct-1")
        win._current_folder = "INBOX"

    def test_partial_scope_does_not_guess_rows_from_cache(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._clear_move_undo = mock.Mock()  # type: ignore[method-assign]
        win._dismiss_bulk_archive_progress_toast = mock.Mock()  # type: ignore[method-assign]
        win._message_total = 2
        cached = [
            {"uid": "1", "subject": "Read", "flags": {"seen": True, "flagged": False}},
            {"uid": "2", "subject": "Unread", "flags": {"seen": False, "flagged": False}},
        ]
        win._current_folder_messages = list(cached)
        MainWindow._on_sidebar_move_started(
            win, "acct-1", "INBOX", "read_unflagged", ""
        )
        win._message_list_view.remove_uids.assert_not_called()
        win._message_list_view.clear.assert_not_called()
        self.assertEqual(win._current_folder_messages, cached)
        self.assertEqual(win._suppress_sync_list_reload, ("acct-1", "INBOX"))

    def test_archive_all_still_clears_the_open_list(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._clear_move_undo = mock.Mock()  # type: ignore[method-assign]
        win._dismiss_bulk_archive_progress_toast = mock.Mock()  # type: ignore[method-assign]
        win._message_total = 2
        win._message_list_view.item_count.return_value = 0
        win._current_folder_messages = [
            {"uid": "1", "subject": "Read", "flags": {"seen": True}},
        ]
        MainWindow._on_sidebar_move_started(win, "acct-1", "INBOX", "all", "")
        win._message_list_view.clear.assert_called_once()
        self.assertEqual(win._current_folder_messages, [])
        self.assertEqual(win._message_total, 0)

    def test_zero_count_bulk_archive_reconciles(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._dismiss_bulk_archive_progress_toast = mock.Mock()  # type: ignore[method-assign]
        win._suppress_sync_list_reload = ("acct-1", "INBOX")
        win._reconcile_open_folder_list = mock.Mock()  # type: ignore[method-assign]
        win._load_messages = mock.Mock()  # type: ignore[method-assign]
        MainWindow._on_sidebar_bulk_archive_finished(
            win, "acct-1", "INBOX", {"archived_count": 0}, "Nothing to archive"
        )
        win._reconcile_open_folder_list.assert_called_once_with("acct-1", "INBOX")
        win._load_messages.assert_not_called()
        self.assertIsNone(win._suppress_sync_list_reload)

    def test_single_move_keeps_selection_removal_and_reconciles(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._message_total = 2
        win._current_folder_messages = [
            {"uid": "1", "subject": "A"},
            {"uid": "2", "subject": "B"},
        ]
        win._message_list_view.remove_uids.return_value = 1
        win._message_list_view.item_count.return_value = 1
        win._suppress_sync_list_reload = ("acct-1", "INBOX")
        win._update_sidebar_from_move_result = mock.Mock()  # type: ignore[method-assign]
        win._finalize_move_status_and_undo = mock.Mock()  # type: ignore[method-assign]
        win._notify_reader_windows_message_moved = mock.Mock()  # type: ignore[method-assign]
        win._reconcile_open_folder_list = mock.Mock()  # type: ignore[method-assign]
        MainWindow._on_messages_moved(
            win,
            "acct-1",
            "INBOX",
            ["1"],
            "Archive",
            {"queued": True, "moved_uids": ["1"]},
            None,
        )
        win._message_list_view.remove_uids.assert_called_once_with(["1"])
        self.assertEqual(
            [message["uid"] for message in win._current_folder_messages],
            ["2"],
        )
        win._reconcile_open_folder_list.assert_called_once_with("acct-1", "INBOX")
        self.assertEqual(win._suppress_sync_list_reload, ("acct-1", "INBOX"))

    def test_reconcile_fetches_a_local_page_off_the_gtk_thread(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._current_folder_messages = [{"uid": "1", "subject": "A"}]
        win._mail = mock.Mock()
        page = ([{"uid": "1", "subject": "A", "flags": {"seen": True}}], 0, 1, "cache")
        win._mail.get_folder_messages.return_value = page
        scheduled: list = []

        def submit(_name: str, func, *args, **kwargs) -> None:
            scheduled.append(func)

        win._mail.submit_interactive.side_effect = submit
        MainWindow._reconcile_open_folder_list(win, "acct-1", "INBOX")
        win._mail.get_folder_messages.assert_not_called()
        self.assertEqual(len(scheduled), 1)
        with mock.patch("post.window.GLib.idle_add") as idle_add:
            scheduled[0]()
        win._mail.get_folder_messages.assert_called_once_with(
            "acct-1",
            "INBOX",
            sync=False,
            limit=500,
        )
        idle_add.assert_called_once()
        self.assertTrue(win._folder_reconcile_inflight)

    def test_equivalent_page_patches_flags_without_rebinding(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._message_total = 2
        win._current_folder_messages = [
            {"uid": "1", "subject": "A", "flags": {"seen": False, "flagged": False}},
            {"uid": "2", "subject": "B", "flags": {"seen": True, "flagged": False}},
        ]
        refreshed = [
            {"uid": "1", "subject": "A", "flags": {"seen": True, "flagged": False}},
            {"uid": "2", "subject": "B", "flags": {"seen": True, "flagged": False}},
        ]
        account = win._current_account
        assert account is not None
        MainWindow._apply_reconciled_folder_page(
            win, account, "INBOX", refreshed, 0, 2
        )
        win._message_list_view.update_message_flags.assert_called_once_with(
            "1", {"seen": True}
        )
        win._apply_messages_to_list.assert_not_called()
        win._message_list_view.clear.assert_not_called()
        self.assertEqual(win._messages_load_generation, 1)

    def test_page_missing_archived_uids_rebinds(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._message_total = 2
        win._current_folder_messages = [
            {"uid": "1", "subject": "Read", "flags": {"seen": True, "flagged": False}},
            {"uid": "2", "subject": "Unread", "flags": {"seen": False, "flagged": False}},
        ]
        remaining = [
            {"uid": "2", "subject": "Unread", "flags": {"seen": False, "flagged": False}},
        ]
        account = win._current_account
        assert account is not None
        MainWindow._apply_reconciled_folder_page(
            win, account, "INBOX", remaining, 1, 1
        )
        win._apply_messages_to_list.assert_called_once()
        applied = win._apply_messages_to_list.call_args.args[0]
        self.assertEqual([message["uid"] for message in applied], ["2"])
        win._message_list_view.update_message_flags.assert_not_called()
        self.assertEqual(
            [message["uid"] for message in win._current_folder_messages or []],
            ["2"],
        )
        self.assertEqual(win._message_total, 1)
        win._message_stack.set_visible_child_name.assert_called_with("list")

    def test_reconcile_timer_starts_and_stops_with_the_open_folder_poll(self) -> None:
        win = self._window()
        win._open_folder_poll_timer_id = None
        win._open_folder_reconcile_timer_id = None
        with mock.patch("post.window.GLib") as glib:
            glib.timeout_add_seconds.side_effect = [11, 22]
            MainWindow._start_open_folder_poll(win)
            intervals = [call.args[0] for call in glib.timeout_add_seconds.call_args_list]
            self.assertEqual(intervals, [5, 30])
            self.assertEqual(win._open_folder_reconcile_timer_id, 11)
            self.assertEqual(win._open_folder_poll_timer_id, 22)
            MainWindow._stop_open_folder_poll(win)
        glib.source_remove.assert_any_call(11)
        glib.source_remove.assert_any_call(22)
        self.assertIsNone(win._open_folder_reconcile_timer_id)
        self.assertIsNone(win._open_folder_poll_timer_id)

    def test_reconcile_tick_skips_while_list_is_populating_or_searching(self) -> None:
        win = self._window()
        self._viewing_inbox(win)
        win._sync_watcher = mock.Mock(running=True)
        win._open_folder_reconcile_timer_id = 7
        win._reconcile_open_folder_list = mock.Mock()  # type: ignore[method-assign]
        win._message_list_populating = True
        self.assertTrue(MainWindow._on_open_folder_reconcile_tick(win))
        win._message_list_populating = False
        win._search_query = object()
        self.assertTrue(MainWindow._on_open_folder_reconcile_tick(win))
        win._reconcile_open_folder_list.assert_not_called()
        win._search_query = None
        self.assertTrue(MainWindow._on_open_folder_reconcile_tick(win))
        win._reconcile_open_folder_list.assert_called_once_with("acct-1", "INBOX")

    def test_reconcile_tick_stops_when_the_watcher_stops(self) -> None:
        win = self._window()
        win._sync_watcher = mock.Mock(running=False)
        win._open_folder_reconcile_timer_id = 7
        self.assertFalse(MainWindow._on_open_folder_reconcile_tick(win))
        self.assertIsNone(win._open_folder_reconcile_timer_id)


if __name__ == "__main__":
    unittest.main()
