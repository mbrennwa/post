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


if __name__ == "__main__":
    unittest.main()
