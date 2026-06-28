# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for offline body sync preference helpers."""

from __future__ import annotations

import tempfile
import unittest

from post import preferences


class OfflineBodySyncPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_path = preferences._PREF_PATH
        preferences._PREF_PATH = f"{self._tmpdir.name}/preferences.json"

    def tearDown(self) -> None:
        preferences._PREF_PATH = self._old_path
        self._tmpdir.cleanup()

    def test_default_is_off(self) -> None:
        self.assertEqual(
            preferences.get_account_offline_body_sync("acct-1"),
            preferences.OFFLINE_BODY_SYNC_OFF,
        )

    def test_set_and_get_mode(self) -> None:
        preferences.set_account_offline_body_sync(
            "acct-1", preferences.OFFLINE_BODY_SYNC_LAST_MONTH
        )
        self.assertEqual(
            preferences.get_account_offline_body_sync("acct-1"),
            preferences.OFFLINE_BODY_SYNC_LAST_MONTH,
        )

    def test_off_removes_entry(self) -> None:
        preferences.set_account_offline_body_sync(
            "acct-1", preferences.OFFLINE_BODY_SYNC_ALL
        )
        preferences.set_account_offline_body_sync(
            "acct-1", preferences.OFFLINE_BODY_SYNC_OFF
        )
        self.assertEqual(
            preferences.get_all_offline_body_sync_modes(),
            {},
        )

    def test_prompt_seen_flag(self) -> None:
        self.assertFalse(preferences.get_offline_body_sync_prompt_seen())
        preferences.set_offline_body_sync_prompt_seen(True)
        self.assertTrue(preferences.get_offline_body_sync_prompt_seen())


if __name__ == "__main__":
    unittest.main()
