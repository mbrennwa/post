# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from post.mail.sync_watcher import MailSyncWatcher


class MailSyncWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = mock.Mock()
        self.mail.get_store_for_sync_if_ready.return_value = object()
        self.mail.get_inbox_folder_name_cached.return_value = "INBOX"
        self.on_folder_changed = mock.Mock()
        self.on_folder_tree_changed = mock.Mock()
        self.watcher = MailSyncWatcher(
            self.mail,
            on_folder_changed=self.on_folder_changed,
            on_folder_tree_changed=self.on_folder_tree_changed,
        )

    def test_start_registers_resume_callback(self) -> None:
        with mock.patch("post.mail.sync_watcher.get_mail_io_thread") as get_thread:
            get_thread.return_value = mock.Mock()
            self.watcher.start()
        self.mail.set_sync_setup_resume_callback.assert_called_once()
        self.watcher.stop()

    def test_stop_clears_resume_callback(self) -> None:
        with mock.patch("post.mail.sync_watcher.get_mail_io_thread") as get_thread:
            get_thread.return_value = mock.Mock()
            self.watcher.start()
            self.watcher.stop()
        self.mail.set_sync_setup_resume_callback.assert_called_with(None)

    def test_yield_schedules_setup_retry(self) -> None:
        idle_callbacks: list[tuple[object, tuple[object, ...]]] = []

        def capture_idle(func: object, *args: object) -> int:
            idle_callbacks.append((func, args))
            return len(idle_callbacks)

        with mock.patch(
            "post.mail.sync_watcher.get_mail_io_thread"
        ) as get_thread, mock.patch(
            "post.mail.sync_watcher.GLib.idle_add", side_effect=capture_idle
        ), mock.patch(
            "post.mail.sync_watcher.account_is_user_offline", return_value=False
        ):
            io_thread = mock.Mock()
            io_thread.has_interactive_work_pending.return_value = True
            get_thread.return_value = io_thread

            self.watcher.set_accounts(["acct-1"])
            self.watcher.start()
            worker = io_thread.submit_background.call_args[0][0]
            worker()

        retry_calls = [args for _func, args in idle_callbacks if args and args[0] == "yield"]
        self.assertEqual(retry_calls, [("yield",)])

    def test_store_not_ready_schedules_setup_retry(self) -> None:
        self.mail.get_store_for_sync_if_ready.return_value = None
        idle_callbacks: list[tuple[object, tuple[object, ...]]] = []

        def capture_idle(func: object, *args: object) -> int:
            idle_callbacks.append((func, args))
            return len(idle_callbacks)

        with mock.patch(
            "post.mail.sync_watcher.get_mail_io_thread"
        ) as get_thread, mock.patch(
            "post.mail.sync_watcher.GLib.idle_add", side_effect=capture_idle
        ), mock.patch(
            "post.mail.sync_watcher.account_is_user_offline", return_value=False
        ):
            io_thread = mock.Mock()
            io_thread.has_interactive_work_pending.return_value = False
            get_thread.return_value = io_thread

            self.watcher.set_accounts(["acct-1"])
            self.watcher.start()
            worker = io_thread.submit_background.call_args[0][0]
            worker()

        retry_calls = [
            args for _func, args in idle_callbacks if args and args[0] == "store_not_ready"
        ]
        self.assertEqual(retry_calls, [("store_not_ready",)])

    def test_background_resume_retries_setup(self) -> None:
        with mock.patch.object(
            self.watcher, "_schedule_setup_retry"
        ) as retry_mock:
            self.watcher._on_background_resume_retry()
        retry_mock.assert_called_once_with("background_resume")


class MainWindowSyncRefreshTests(unittest.TestCase):
    def test_resolved_sync_folder_name_maps_inbox_alias(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._sidebar = mock.Mock()
        window._sidebar.inbox_folder_for_account.return_value = "mailFolders/Inbox"

        self.assertEqual(
            MainWindow._resolved_sync_folder_name(window, "acct-1", "INBOX"),
            "mailFolders/Inbox",
        )
        self.assertEqual(
            MainWindow._resolved_sync_folder_name(window, "acct-1", "Sent"),
            "Sent",
        )

    def test_is_viewing_folder_matches_inbox_alias(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._current_account = mock.Mock(uid="acct-1")
        window._current_folder = "INBOX"
        window._sidebar = mock.Mock()
        window._sidebar.inbox_folder_for_account.return_value = "mailFolders/Inbox"

        self.assertTrue(
            MainWindow._is_viewing_folder(window, "acct-1", "mailFolders/Inbox")
        )
        self.assertFalse(
            MainWindow._is_viewing_folder(window, "acct-1", "Sent")
        )

    def test_maybe_run_pending_sync_folder_refresh(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._pending_sync_folder_refresh = ("acct-1", "INBOX")
        window._message_list_populating = False
        window._is_viewing_folder = mock.Mock(return_value=True)
        window._sync_current_folder_messages = mock.Mock()

        MainWindow._maybe_run_pending_sync_folder_refresh(window)

        window._sync_current_folder_messages.assert_called_once_with(
            "acct-1", "INBOX"
        )
        self.assertIsNone(window._pending_sync_folder_refresh)


if __name__ == "__main__":
    unittest.main()
