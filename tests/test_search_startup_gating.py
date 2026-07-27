# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.mail.eds import MailAccount
from post.mail.search import parse_search_query
from post.preferences import (
    SEARCH_SCOPE_ACCOUNT,
    SEARCH_SCOPE_ALL,
    SEARCH_SCOPE_FOLDER,
    SearchScope,
)
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

    def test_not_ready_before_first_folder_load_finishes(self) -> None:
        # pending==0 before load() must not look "ready" (eager restore race).
        self.assertEqual(self.sidebar._folder_loads_pending, 0)
        self.assertFalse(self.sidebar.folder_tree_ready)

    def test_not_ready_while_folder_loads_pending(self) -> None:
        self.sidebar._folder_loads_pending = 2
        self.assertFalse(self.sidebar.folder_tree_ready)

    def test_load_clears_ready_until_finish(self) -> None:
        self.sidebar._folder_tree_ready = True
        self.sidebar._mail.list_accounts.return_value = [_account("acct-1")]
        with (
            mock.patch.object(self.sidebar, "_start_folder_load"),
            mock.patch.object(
                self.sidebar,
                "_make_account_section_loading",
                return_value=Gtk.Box(),
            ),
        ):
            self.sidebar.load()
        self.assertFalse(self.sidebar.folder_tree_ready)
        self.assertEqual(self.sidebar._folder_loads_pending, 1)

        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self.assertTrue(self.sidebar.folder_tree_ready)

    def test_load_reports_startup_folder_status_progress(self) -> None:
        set_status = self.sidebar._set_status
        self.sidebar._mail.list_accounts.return_value = [
            _account("acct-1"),
            _account("acct-2"),
        ]
        with (
            mock.patch.object(self.sidebar, "_start_folder_load"),
            mock.patch.object(
                self.sidebar,
                "_make_account_section_loading",
                side_effect=lambda *_a, **_k: Gtk.Box(),
            ),
            mock.patch.object(
                self.sidebar,
                "_make_inbox_section_loading",
                side_effect=lambda *_a, **_k: Gtk.Box(),
            ),
            mock.patch.object(self.sidebar, "_add_inbox_row_unavailable"),
        ):
            self.sidebar.load()

        set_status.assert_called_with("Loading folders for 0 of 2 accounts…")
        self.assertEqual(self.sidebar._startup_folder_total, 2)
        self.assertEqual(self.sidebar._folder_loads_pending, 2)

        self.sidebar._folder_loads_pending = 1
        self.sidebar._update_startup_folder_load_status()
        set_status.assert_called_with("Loading folders for 1 of 2 accounts…")

        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        set_status.assert_called_with("2 account(s)")
        self.assertEqual(self.sidebar._startup_folder_total, 0)
        self.assertTrue(self.sidebar.folder_tree_ready)

    def test_folder_tree_ready_callback_fires_when_loads_complete(self) -> None:
        self.sidebar._folder_loads_pending = 1
        self.sidebar._maybe_finish_initial_folder_load()
        self._on_folder_tree_ready.assert_not_called()
        self.assertFalse(self.sidebar.folder_tree_ready)

        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self._on_folder_tree_ready.assert_called_once_with()
        self.assertTrue(self.sidebar.folder_tree_ready)

    def test_folder_tree_ready_callback_fires_on_each_completion(self) -> None:
        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self.sidebar._maybe_finish_initial_folder_load()
        self.assertEqual(self._on_folder_tree_ready.call_count, 2)
        self.assertTrue(self.sidebar.folder_tree_ready)

    def test_folder_tree_ready_runs_before_initial_load_complete(self) -> None:
        order: list[str] = []
        self.sidebar._on_folder_tree_ready = lambda: order.append("ready")
        self.sidebar._on_initial_folder_load_complete = lambda: order.append(
            "offline"
        )
        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        self.assertEqual(order, ["ready", "offline"])

    def test_folder_tree_ready_still_runs_if_initial_load_complete_raises(self) -> None:
        ready = mock.Mock()

        def boom() -> None:
            raise RuntimeError("offline sync setup failed")

        self.sidebar._on_folder_tree_ready = ready
        self.sidebar._on_initial_folder_load_complete = boom
        self.sidebar._folder_loads_pending = 0
        self.sidebar._maybe_finish_initial_folder_load()
        ready.assert_called_once_with()
        self.assertTrue(self.sidebar.folder_tree_ready)
        self.assertIsNone(self.sidebar._on_initial_folder_load_complete)

    def test_error_path_selects_before_marking_folder_tree_ready(self) -> None:
        order: list[str] = []
        self.sidebar._folder_loads_pending = 1
        self.sidebar._needs_initial_selection = True
        self.sidebar._accounts_by_uid["acct-1"] = _account("acct-1")
        self.sidebar._folder_lists["acct-1"] = Gtk.ListBox()
        self.sidebar._on_folder_tree_ready = lambda: order.append("ready")

        def apply_selection() -> None:
            order.append("select")
            self.sidebar._needs_initial_selection = False

        with (
            mock.patch.object(
                self.sidebar,
                "_maybe_apply_initial_selection",
                side_effect=apply_selection,
            ),
            mock.patch.object(self.sidebar, "_finish_account_reload"),
            mock.patch.object(self.sidebar, "_update_startup_folder_load_status"),
            mock.patch.object(self.sidebar, "_clear_listbox"),
            mock.patch.object(
                self.sidebar, "_add_outbox_row"
            ),
            mock.patch.object(
                self.sidebar, "_add_inbox_row_unavailable"
            ),
            mock.patch.object(
                self.sidebar, "_update_account_offline_marker"
            ),
            mock.patch.object(
                self.sidebar, "_wrap_list_row", return_value=Gtk.ListBoxRow()
            ),
            mock.patch(
                "post.sidebar.format_folder_load_error",
                return_value="failed",
            ),
            mock.patch(
                "post.sidebar.is_network_unavailable_error",
                return_value=False,
            ),
            mock.patch(
                "post.sidebar.is_sign_in_required_error",
                return_value=False,
            ),
        ):
            self.sidebar._on_folders_loaded(
                self.sidebar._load_generation,
                "acct-1",
                None,
                RuntimeError("boom"),
            )

        self.assertEqual(order, ["select", "ready"])
        self.assertTrue(self.sidebar.folder_tree_ready)


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

    def test_folder_tree_ready_enables_search_without_user_click(self) -> None:
        """Eager-restored folder + tree ready must enable search automatically (#196)."""
        self.window._header_search_entry.set_sensitive(False)
        self.window._search_scope_dropdown.set_sensitive(False)
        self.window._sidebar.folder_tree_ready = True
        self.window._sync_watcher = mock.Mock()
        self.window._sync_watcher.running = False
        self.window._folder_count_poll_deferred_id = None
        self.window._update_search_entry_state = (
            lambda: MainWindow._update_search_entry_state(self.window)
        )

        MainWindow._on_folder_tree_ready(self.window)

        self.assertTrue(self.window._header_search_entry.get_sensitive())
        self.assertTrue(self.window._search_scope_dropdown.get_sensitive())

    def test_reselecting_current_folder_refreshes_search_without_reload(self) -> None:
        self.window._sidebar.folder_tree_ready = True
        self.window._parse_search_from_entry = mock.Mock(return_value=None)
        self.window._prepare_folder_selection = mock.Mock()
        self.window._load_messages = mock.Mock()
        self.window._current_folder_messages = []

        MainWindow._on_folder_selected(
            self.window, self.window._current_account, "INBOX"
        )

        self.window._prepare_folder_selection.assert_called_once_with(
            self.window._current_account, "INBOX"
        )
        self.window._load_messages.assert_not_called()

    def test_selecting_new_folder_loads_messages(self) -> None:
        other = _account("acct-2")
        self.window._parse_search_from_entry = mock.Mock(return_value=None)
        self.window._prepare_folder_selection = mock.Mock()
        self.window._load_messages = mock.Mock()
        self.window._current_folder_messages = []

        MainWindow._on_folder_selected(self.window, other, "INBOX")

        self.window._prepare_folder_selection.assert_called_once_with(other, "INBOX")
        self.window._load_messages.assert_called_once_with(other.uid, "INBOX")

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


class CachedHeaderSearchGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = mock.Mock()
        self.window._search_scope = SearchScope(SEARCH_SCOPE_FOLDER)

    def test_folder_scope_header_query_uses_cache_when_present(self) -> None:
        query = parse_search_query("from:rebecca")
        with mock.patch(
            "post.window.folder_index_has_cache", return_value=True
        ) as has_cache:
            self.assertTrue(
                MainWindow._should_use_cached_header_search(
                    self.window, query, "acct-1", "INBOX"
                )
            )
        has_cache.assert_called_once_with("acct-1", "INBOX")

    def test_all_mail_header_query_skips_cache_fast_path(self) -> None:
        self.window._search_scope = SearchScope(SEARCH_SCOPE_ALL)
        query = parse_search_query("from:rebecca")
        with mock.patch("post.window.folder_index_has_cache", return_value=True):
            self.assertFalse(
                MainWindow._should_use_cached_header_search(
                    self.window, query, "acct-1", "INBOX"
                )
            )

    def test_account_scope_header_query_skips_cache_fast_path(self) -> None:
        self.window._search_scope = SearchScope(
            SEARCH_SCOPE_ACCOUNT, account_uid="acct-other"
        )
        query = parse_search_query("from:rebecca")
        with mock.patch("post.window.folder_index_has_cache", return_value=True):
            self.assertFalse(
                MainWindow._should_use_cached_header_search(
                    self.window, query, "acct-1", "INBOX"
                )
            )

    def test_folder_scope_text_query_skips_cache_fast_path(self) -> None:
        query = parse_search_query("rebecca")
        with mock.patch("post.window.folder_index_has_cache", return_value=True):
            self.assertFalse(
                MainWindow._should_use_cached_header_search(
                    self.window, query, "acct-1", "INBOX"
                )
            )


if __name__ == "__main__":
    unittest.main()
