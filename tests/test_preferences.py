# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from post.preferences import (
    get_account_signature,
    get_account_signatures,
    get_load_remote_content,
    get_show_evolution_local,
    get_sidebar_state,
    get_window_state,
    register_inbox_accounts,
    resolve_inbox_display_order,
    set_account_signature,
    set_active_message_uid,
    set_load_remote_content,
    set_show_evolution_local,
    set_sidebar_state,
    set_window_state,
)


class PreferencesTests(unittest.TestCase):
    def test_missing_file_returns_none(self) -> None:
        with mock.patch(
            "post.preferences._PREF_PATH",
            os.path.join(tempfile.gettempdir(), "post-prefs-missing.json"),
        ):
            self.assertIsNone(get_show_evolution_local())

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                set_show_evolution_local(True)
                self.assertTrue(get_show_evolution_local())
                set_show_evolution_local(False)
                self.assertFalse(get_show_evolution_local())
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertFalse(data["show_evolution_local"])

    def test_load_remote_content_defaults_false(self) -> None:
        with mock.patch(
            "post.preferences._PREF_PATH",
            os.path.join(tempfile.gettempdir(), "post-prefs-remote-missing.json"),
        ):
            self.assertFalse(get_load_remote_content())

    def test_load_remote_content_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                set_load_remote_content(True)
                self.assertTrue(get_load_remote_content())
                set_load_remote_content(False)
                self.assertFalse(get_load_remote_content())

    def test_window_state_defaults(self) -> None:
        with mock.patch(
            "post.preferences._PREF_PATH",
            os.path.join(tempfile.gettempdir(), "post-prefs-window-missing.json"),
        ):
            state = get_window_state()
            self.assertEqual(state["width"], 1100)
            self.assertEqual(state["height"], 720)
            self.assertFalse(state["maximized"])

    def test_window_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                set_window_state(width=1280, height=800, maximized=True)
                state = get_window_state()
                self.assertEqual(state["width"], 1280)
                self.assertEqual(state["height"], 800)
                self.assertTrue(state["maximized"])

    def test_sidebar_state_defaults(self) -> None:
        with mock.patch(
            "post.preferences._PREF_PATH",
            os.path.join(tempfile.gettempdir(), "post-prefs-sidebar-missing.json"),
        ):
            state = get_sidebar_state()
            self.assertTrue(state["inbox_expanded"])
            self.assertEqual(state["accounts"], {})
            self.assertIsNone(state["active_folder"])
            self.assertEqual(state["inbox_order"], [])

    def test_sidebar_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                set_sidebar_state(
                    inbox_expanded=False,
                    accounts={"acct-1": True, "acct-2": False},
                    active_folder=("acct-1", "INBOX"),
                    inbox_order=["acct-2", "acct-1"],
                )
                set_active_message_uid("msg-42")
                state = get_sidebar_state()
                self.assertFalse(state["inbox_expanded"])
                self.assertEqual(
                    state["accounts"], {"acct-1": True, "acct-2": False}
                )
                self.assertEqual(state["active_folder"], ("acct-1", "INBOX"))
                self.assertEqual(state["active_message_uid"], "msg-42")
                self.assertEqual(state["inbox_order"], ["acct-2", "acct-1"])
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertEqual(
                    data["sidebar"]["active_folder"],
                    {
                        "account_uid": "acct-1",
                        "folder_name": "INBOX",
                        "message_uid": "msg-42",
                    },
                )

    def test_set_active_message_uid_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                set_sidebar_state(
                    inbox_expanded=True,
                    accounts={},
                    active_folder=("acct-1", "INBOX"),
                )
                set_active_message_uid("msg-1")
                set_active_message_uid(None)
                state = get_sidebar_state()
                self.assertIsNone(state["active_message_uid"])
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertNotIn("message_uid", data["sidebar"]["active_folder"])

    def test_resolve_inbox_display_order_keeps_unloaded_accounts(self) -> None:
        saved = ["acct-2", "acct-1"]
        present = ["acct-1"]
        self.assertEqual(
            resolve_inbox_display_order(saved, present),
            ["acct-1"],
        )
        self.assertEqual(
            resolve_inbox_display_order(saved, ["acct-1", "acct-2"]),
            ["acct-2", "acct-1"],
        )

    def test_register_inbox_accounts_appends_new(self) -> None:
        self.assertEqual(
            register_inbox_accounts(["acct-2", "acct-1"], ["acct-1"]),
            ["acct-2", "acct-1"],
        )
        self.assertEqual(
            register_inbox_accounts(["acct-2"], ["acct-1", "acct-2"]),
            ["acct-2", "acct-1"],
        )

    def test_account_signatures_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preferences.json")
            with mock.patch("post.preferences._PREF_PATH", path):
                self.assertEqual(get_account_signature("acct-1"), "")
                set_account_signature("acct-1", "Alice\nExample Corp")
                self.assertEqual(
                    get_account_signature("acct-1"),
                    "Alice\nExample Corp",
                )
                self.assertEqual(
                    get_account_signatures(),
                    {"acct-1": "Alice\nExample Corp"},
                )
                set_account_signature("acct-1", "")
                self.assertEqual(get_account_signatures(), {})


if __name__ == "__main__":
    unittest.main()
