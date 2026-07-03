# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.mail.eds import MailAccount
from post.preferences import SEARCH_SCOPE_ALL, SEARCH_SCOPE_FOLDER, SearchScope
from post.sidebar import MailSidebar
from post.window import MainWindow


def _sidebar_state() -> dict:
    return {
        "inbox_expanded": True,
        "accounts": {},
        "active_folder": None,
        "active_message_uid": None,
        "inbox_order": [],
    }


def _account(uid: str) -> MailAccount:
    return MailAccount(
        uid=uid,
        name=f"Account {uid}",
        email=f"{uid}@example.com",
        backend="imapx",
        identity_uid=None,
        from_name=None,
        from_address=None,
        transport_uid=None,
    )


class FolderTreeReadyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self._on_folder_tree_ready = mock.Mock()
        with mock.patch(
            "post.sidebar.get_sidebar_state",
            return_value=_sidebar_state(),
        ):
            self.sidebar = MailSidebar(
                mock.Mock(),
                on_folder_selected=mock.Mock(),
                set_status=mock.Mock(),
                on_folder_tree_ready=self._on_folder_tree_ready,
            )

    def test_ready_when_no_folder_loads_pending(self) -> None:
        self.assertTrue(self.sidebar.folder_tree_ready)

    def test_not_ready_while_folder_loads_pending(self) -> None:
        self.sidebar._folder_loads_pending = 2
        self.assertFalse(self.sidebar.folder_tree_ready)

    def test_folder_tree_ready_callback_fires_when_loads_complete(self) -> None:
        self.sidebar._folder_loads_pending = 1
        self.sidebar._maybe_finish_initial_folder_load()
        self._on_folder_tree_ready.assert_not_called()

        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self._on_folder_tree_ready.assert_called_once_with()

    def test_folder_tree_ready_callback_fires_on_each_completion(self) -> None:
        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self.sidebar._maybe_finish_initial_folder_load()
        self.assertEqual(self._on_folder_tree_ready.call_count, 2)


class SearchEntryStartupGatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.window = mock.Mock()
        self.window._current_account = _account("acct-1")
        self.window._current_folder = "INBOX"
        self.window._search_query = None
        self.window._search_entry_updating = False
        self.window._search_scope = mock.Mock()
        self.window._search_scope.kind = "folder"
        self.window._header_search_entry = Gtk.SearchEntry()
        self.window._search_scope_dropdown = Gtk.DropDown()
        self.window._sidebar = mock.Mock()
        self.window._sidebar.folder_tree_ready = False
        self.window._is_multi_folder_scope = mock.Mock(return_value=False)
        self.window._leave_multi_folder_sidebar_mode = mock.Mock()

    def test_search_entry_disabled_while_folder_tree_loading(self) -> None:
        MainWindow._update_search_entry_state(self.window)
        self.assertFalse(self.window._header_search_entry.get_sensitive())
        self.assertFalse(self.window._search_scope_dropdown.get_sensitive())

    def test_search_entry_enabled_when_folder_tree_ready(self) -> None:
        self.window._sidebar.folder_tree_ready = True
        MainWindow._update_search_entry_state(self.window)
        self.assertTrue(self.window._header_search_entry.get_sensitive())
        self.assertTrue(self.window._search_scope_dropdown.get_sensitive())

    def test_apply_search_skips_while_folder_tree_loading(self) -> None:
        self.window._header_search_entry.set_text("hello")
        self.window._load_messages = mock.Mock()
        self.window._preserve_pre_search_snapshot = mock.Mock()
        self.window._update_search_scope_ui = mock.Mock()
        self.window._restore_messages_after_search = mock.Mock()

        MainWindow._apply_search_from_entry(self.window)

        self.window._load_messages.assert_not_called()
        self.window._preserve_pre_search_snapshot.assert_not_called()

    def test_all_mail_scope_preserved_while_folder_tree_loading(self) -> None:
        self.window._search_scope = SearchScope(SEARCH_SCOPE_ALL)
        self.window._search_scope_items = [
            SearchScope(SEARCH_SCOPE_FOLDER),
            SearchScope(SEARCH_SCOPE_ALL),
        ]
        self.window._is_multi_folder_scope = mock.Mock(return_value=True)
        self.window._set_search_scope_dropdown_selected = mock.Mock()
        self.window._leave_multi_folder_sidebar_mode = mock.Mock()

        with mock.patch("post.window.set_search_scope") as set_search_scope:
            MainWindow._update_search_entry_state(self.window)

        self.assertIs(self.window._search_scope.kind, SEARCH_SCOPE_ALL)
        set_search_scope.assert_not_called()
        self.window._set_search_scope_dropdown_selected.assert_not_called()
        self.window._leave_multi_folder_sidebar_mode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
