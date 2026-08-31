# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from post.mail import correspondent_cache
from post.mail.correspondents import Correspondent


class CorrespondentCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            correspondent_cache,
            "_CACHE_ROOT",
            Path(self._tmpdir.name),
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_save_and_load_round_trip(self) -> None:
        items = [
            Correspondent(
                display="Alice <alice@example.com>",
                email="alice@example.com",
                name="Alice",
                last_seen=42,
            )
        ]
        correspondent_cache.save("acct-1", items)
        loaded = correspondent_cache.load("acct-1")
        self.assertEqual(loaded, items)

    def test_does_not_save_empty(self) -> None:
        correspondent_cache.save("acct-1", [])
        self.assertIsNone(correspondent_cache.load("acct-1"))

    def test_load_missing_is_none(self) -> None:
        self.assertIsNone(correspondent_cache.load("acct-1"))

    def test_invalidate_removes_cache(self) -> None:
        correspondent_cache.save(
            "acct-1",
            [
                Correspondent(
                    display="Alice <alice@example.com>",
                    email="alice@example.com",
                    name="Alice",
                    last_seen=1,
                )
            ],
        )
        correspondent_cache.invalidate("acct-1")
        self.assertIsNone(correspondent_cache.load("acct-1"))

    def test_invalidate_account_removes_directory(self) -> None:
        items = [
            Correspondent(
                display="Alice <alice@example.com>",
                email="alice@example.com",
                name="Alice",
                last_seen=1,
            )
        ]
        correspondent_cache.save("acct-1", items)
        correspondent_cache.save("acct-2", items)
        self.assertEqual(
            set(correspondent_cache.cached_account_uids()),
            {"acct-1", "acct-2"},
        )
        correspondent_cache.invalidate_account("acct-1")
        self.assertIsNone(correspondent_cache.load("acct-1"))
        self.assertEqual(correspondent_cache.load("acct-2"), items)
        self.assertFalse(
            (Path(self._tmpdir.name) / "acct-1").exists()
        )


if __name__ == "__main__":
    unittest.main()
