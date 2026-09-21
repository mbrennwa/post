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
        self.watcher.start()
        self.mail.set_sync_setup_resume_callback.assert_called_once()
        self.watcher.stop()

    def test_stop_clears_resume_callback(self) -> None:
        self.watcher.start()
        self.watcher.stop()
        self.mail.set_sync_setup_resume_callback.assert_called_with(None)

    def test_yield_schedules_setup_retry(self) -> None:
        idle_callbacks: list[tuple[object, tuple[object, ...]]] = []

        def capture_idle(func: object, *args: object) -> int:
            idle_callbacks.append((func, args))
            return len(idle_callbacks)

        with mock.patch(
            "post.mail.sync_watcher.GLib.idle_add", side_effect=capture_idle
        ), mock.patch(
            "post.mail.sync_watcher.account_is_user_offline", return_value=False
        ):
            self.mail.has_interactive_work_pending.return_value = True

            self.watcher.set_accounts(["acct-1"])
            self.watcher.start()
            worker = self.mail.submit_background.call_args[0][1]
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
            "post.mail.sync_watcher.GLib.idle_add", side_effect=capture_idle
        ), mock.patch(
            "post.mail.sync_watcher.account_is_user_offline", return_value=False
        ):
            self.mail.has_interactive_work_pending.return_value = False

            self.watcher.set_accounts(["acct-1"])
            self.watcher.start()
            worker = self.mail.submit_background.call_args[0][1]
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

    def test_backend_uses_open_folder_poll(self) -> None:
        from post.window import MainWindow

        self.assertTrue(MainWindow._backend_uses_open_folder_poll("imapx"))
        self.assertTrue(MainWindow._backend_uses_open_folder_poll("IMAP"))
        self.assertTrue(MainWindow._backend_uses_open_folder_poll("microsoft365"))
        self.assertTrue(MainWindow._backend_uses_open_folder_poll("ews"))
        self.assertFalse(MainWindow._backend_uses_open_folder_poll("spool"))
        self.assertFalse(MainWindow._backend_uses_open_folder_poll(None))

    def test_open_folder_poll_tick_syncs_imapx(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._sync_watcher = mock.Mock(running=True)
        window._open_folder_poll_timer_id = 1
        window._current_account = mock.Mock(uid="acct-1", backend="imapx")
        window._current_folder = "INBOX"
        window._network_available = True
        window._account_server_sync_enabled = mock.Mock(return_value=True)
        window._search_query = None
        window._message_sync_in_progress = False
        window._message_list_populating = False
        window._sync_current_folder_messages = mock.Mock()

        with mock.patch("post.window.is_heavy_folder_name", return_value=False), mock.patch(
            "post.window.is_post_outbox_folder", return_value=False
        ):
            self.assertTrue(MainWindow._on_open_folder_poll_tick(window))

        window._sync_current_folder_messages.assert_called_once_with("acct-1", "INBOX")

    def test_open_folder_poll_tick_skips_spool(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._sync_watcher = mock.Mock(running=True)
        window._open_folder_poll_timer_id = 1
        window._current_account = mock.Mock(uid="acct-1", backend="spool")
        window._current_folder = "INBOX"
        window._sync_current_folder_messages = mock.Mock()

        self.assertTrue(MainWindow._on_open_folder_poll_tick(window))
        window._sync_current_folder_messages.assert_not_called()

    def test_sync_folder_changed_queues_pending_while_sync_in_progress(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._message_sync_in_progress = True
        window._current_account = mock.Mock(uid="acct-1")
        window._current_folder = "INBOX"
        window._pending_sync_folder_refresh = None
        window._sidebar = mock.Mock()
        window._is_viewing_folder = mock.Mock(return_value=True)
        window._mail = mock.Mock()
        window._refresh_folder_view = mock.Mock()

        with mock.patch("post.window.is_heavy_folder_name", return_value=False), mock.patch(
            "post.window.search_trace"
        ):
            MainWindow._on_sync_folder_changed(window, "acct-1", "INBOX")

        self.assertEqual(window._pending_sync_folder_refresh, ("acct-1", "INBOX"))
        window._sidebar.refresh_folder_row.assert_called_once_with("acct-1", "INBOX")
        window._refresh_folder_view.assert_not_called()
        window._mail.invalidate_folder_index.assert_not_called()

    def test_messages_sync_finished_runs_pending_refresh(self) -> None:
        from post.window import MainWindow

        class Stub:
            pass

        window = Stub()
        window._messages_load_generation = 3
        window._message_sync_in_progress = True
        window._current_account = mock.Mock(uid="acct-1")
        window._current_folder = "INBOX"
        window._pending_sync_folder_refresh = ("acct-1", "INBOX")
        window._message_list_populating = False
        window._is_viewing_folder = mock.Mock(return_value=True)
        window._sync_current_folder_messages = mock.Mock()
        window._release_offline_sync_for_folder_work = mock.Mock()
        window._update_message_status = mock.Mock()
        window._maybe_run_pending_sync_folder_refresh = (
            lambda: MainWindow._maybe_run_pending_sync_folder_refresh(window)
        )

        MainWindow._on_messages_sync_finished(window, 3, False)

        self.assertFalse(window._message_sync_in_progress)
        window._sync_current_folder_messages.assert_called_once_with("acct-1", "INBOX")
        self.assertIsNone(window._pending_sync_folder_refresh)


if __name__ == "__main__":
    unittest.main()
