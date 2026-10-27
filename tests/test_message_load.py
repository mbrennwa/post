# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.mail.eds import MailAccount
from post.window import MainWindow


def _account(uid: str = "acct-1") -> MailAccount:
    return MailAccount(
        uid=uid,
        name="Test",
        email="user@example.com",
        backend="imapx",
        identity_uid=None,
        from_name=None,
        from_address=None,
        transport_uid=None,
    )


class EmptyFolderLoadCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def _window(self) -> mock.Mock:
        window = mock.Mock()
        window._messages_load_generation = 7
        window._messages_load_expects_search = False
        window._search_query = None
        window._search_results_streamed = False
        window._current_account = _account()
        window._current_folder = "Inbox"
        window._current_folder_messages = None
        window._message_total = -1
        window._message_list_source = "disk_cache"
        window._message_sync_in_progress = False
        window._message_list_view.item_count.return_value = 0
        window._apply_pending_flag_overlay.side_effect = (
            lambda _account, _folder, messages, unread: (messages, unread)
        )
        return window

    def test_empty_disk_cache_still_runs_after_list_callback(self) -> None:
        """Empty folder cache must still start background sync (#339)."""
        window = self._window()
        after_list = mock.Mock()

        result = MainWindow._on_messages_loaded(
            window,
            7,
            "acct-1",
            "Inbox",
            [],
            0,
            0,
            "disk_cache",
            True,
            None,
            after_list,
        )

        self.assertFalse(result)
        after_list.assert_called_once_with()
        window._message_stack.set_visible_child_name.assert_called_with("empty")
        self.assertTrue(window._message_sync_in_progress)
        window._release_offline_sync_for_folder_work.assert_not_called()

    def test_empty_folder_without_callback_does_not_raise(self) -> None:
        window = self._window()

        result = MainWindow._on_messages_loaded(
            window,
            7,
            "acct-1",
            "Inbox",
            [],
            0,
            0,
            "disk_cache",
            False,
            None,
            None,
        )

        self.assertFalse(result)
        window._message_stack.set_visible_child_name.assert_called_with("empty")
        window._release_offline_sync_for_folder_work.assert_called_once_with(7)

    def test_streamed_search_preserve_still_runs_after_list_callback(self) -> None:
        window = self._window()
        window._messages_load_expects_search = True
        window._search_query = mock.Mock()
        window._search_results_streamed = True
        window._message_list_view.item_count.return_value = 3
        after_list = mock.Mock()

        result = MainWindow._on_messages_loaded(
            window,
            7,
            "acct-1",
            "Inbox",
            [],
            0,
            0,
            "disk_cache",
            True,
            None,
            after_list,
        )

        self.assertFalse(result)
        after_list.assert_called_once_with()
        window._message_stack.set_visible_child_name.assert_called_with("list")
        window._release_offline_sync_for_folder_work.assert_not_called()


class HeavyFolderOfflineHoldTests(unittest.TestCase):
    def _window(self, *, indexing: bool) -> mock.Mock:
        window = mock.Mock()
        window._messages_load_generation = 7
        window._offline_held_for_load_generation = 7
        window._offline_held_account_uid = "acct-1"
        window._current_account = _account()
        window._current_folder = "Archive"
        window._heavy_index_in_progress = (
            ("acct-1", "Archive") if indexing else None
        )
        window._offline_download_status = "Downloading…"
        window._mail = mock.Mock()
        window._heavy_index_blocks_offline_sync = (
            lambda load_id: MainWindow._heavy_index_blocks_offline_sync(
                window, load_id
            )
        )
        return window

    def test_keeps_hold_while_heavy_index_in_progress(self) -> None:
        """Archive indexing must keep that account's downsync hold (#407/#449)."""
        window = self._window(indexing=True)

        MainWindow._release_offline_sync_for_folder_work(window, 7)

        window._mail.hold_offline_body_sync.assert_not_called()
        self.assertEqual(window._offline_held_for_load_generation, 7)
        self.assertEqual(window._offline_held_account_uid, "acct-1")
        window._sync_watcher_current_folder.assert_not_called()

    def test_releases_hold_when_archive_selected_but_index_done(self) -> None:
        """Caught-up Archive must not keep that account's backfill paused (#407/#449)."""
        window = self._window(indexing=False)

        MainWindow._release_offline_sync_for_folder_work(window, 7)

        window._mail.hold_offline_body_sync.assert_called_once_with(
            False, account_uid="acct-1"
        )
        self.assertIsNone(window._offline_held_for_load_generation)
        self.assertIsNone(window._offline_held_account_uid)
        window._sync_watcher_current_folder.assert_called_once_with()

    def test_hold_releases_previous_account_when_switching(self) -> None:
        window = self._window(indexing=False)
        window._offline_held_account_uid = "acct-old"
        window._offline_held_for_load_generation = 6

        MainWindow._hold_offline_sync_for_folder_work(window, 8, "acct-1")

        window._mail.hold_offline_body_sync.assert_any_call(
            False, account_uid="acct-old"
        )
        window._mail.hold_offline_body_sync.assert_any_call(
            True, account_uid="acct-1"
        )
        self.assertEqual(window._offline_held_for_load_generation, 8)
        self.assertEqual(window._offline_held_account_uid, "acct-1")


class HeavyIndexResumeAfterReadTests(unittest.TestCase):
    def _window(self) -> mock.Mock:
        window = mock.Mock()
        window._mail = mock.Mock()
        window._heavy_index_in_progress = ("acct", "Archive")
        window._search_query = None
        window._current_account = mock.Mock(uid="acct")
        window._current_folder = "Archive"
        window._messages_load_generation = 3
        window._heavy_index_still_active = (
            lambda account_uid, folder_name: MainWindow._heavy_index_still_active(
                window, account_uid, folder_name
            )
        )
        window._adopt_heavy_index_load_id = (
            lambda load_id, account_uid, folder_name: (
                MainWindow._adopt_heavy_index_load_id(
                    window, load_id, account_uid, folder_name
                )
            )
        )
        window._start_background_heavy_folder_index = mock.Mock()
        return window

    def test_resume_waits_until_the_read_finishes(self) -> None:
        window = self._window()
        window._mail.has_interactive_work_pending.return_value = True
        with mock.patch("post.window.GLib.timeout_add") as timeout_add:
            MainWindow._resume_heavy_index_when_idle(
                window,
                3,
                "acct",
                "Archive",
                {"pending_server_refresh": True},
                delay_ms=250,
            )
        timeout_add.assert_called_once()
        delay, callback = timeout_add.call_args[0][:2]
        self.assertEqual(delay, 250)
        self.assertTrue(callback())
        window._start_background_heavy_folder_index.assert_not_called()
        window._mail.has_interactive_work_pending.return_value = False
        self.assertFalse(callback())
        window._start_background_heavy_folder_index.assert_called_once_with(
            3,
            "acct",
            "Archive",
            cursor={"pending_server_refresh": True},
        )

    def test_finished_slice_does_not_restart_while_a_read_is_pending(self) -> None:
        window = self._window()
        window._mail.has_interactive_work_pending.return_value = True
        progress = mock.Mock(done=False, cursor={})
        window._mail.continue_heavy_folder_index.return_value = progress
        with (
            mock.patch("post.window.GLib.timeout_add"),
            mock.patch("post.window.GLib.idle_add"),
        ):
            MainWindow._run_heavy_folder_index_slice(
                window, 3, "acct", "Archive", None
            )
        window._resume_heavy_index_when_idle.assert_called_once_with(
            3,
            "acct",
            "Archive",
            {},
            delay_ms=250,
        )
        window._mail.submit_background.assert_not_called()


if __name__ == "__main__":
    unittest.main()
