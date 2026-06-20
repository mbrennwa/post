# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from post.preferences import (
    get_load_remote_content,
    get_show_evolution_local,
    set_load_remote_content,
    set_show_evolution_local,
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


if __name__ == "__main__":
    unittest.main()
