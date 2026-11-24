# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
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
                sent_count=3,
                received_count=5,
                last_sent=40,
                last_received=42,
            )
        ]
        correspondent_cache.save("acct-1", items)
        loaded = correspondent_cache.load("acct-1")
        self.assertEqual(loaded, items)

    def test_folder_contributions_round_trip(self) -> None:
        item = Correspondent(
            display="Alice <alice@example.com>",
            email="alice@example.com",
            name="Alice",
            last_seen=9,
            sent_count=1,
            last_sent=9,
        )
        folders = {"Sent": [item], "INBOX": []}
        correspondent_cache.save("acct-1", [item], folders=folders)
        self.assertEqual(correspondent_cache.load_folders("acct-1"), folders)

    def test_load_folders_none_without_folders(self) -> None:
        correspondent_cache.save(
            "acct-1",
            [Correspondent(display="a@example.com", email="a@example.com", name="")],
        )
        self.assertIsNone(correspondent_cache.load_folders("acct-1"))

    def _write_v1(self) -> None:
        path = correspondent_cache._cache_path("acct-1")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "correspondents": [
                        {
                            "display": "Alice <alice@example.com>",
                            "email": "alice@example.com",
                            "name": "Alice",
                            "last_seen": 1,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_v1_cache_is_still_readable(self) -> None:
        self._write_v1()
        loaded = correspondent_cache.load("acct-1")
        self.assertEqual([item.email for item in loaded], ["alice@example.com"])
        self.assertEqual(loaded[0].sent_count, 0)
        self.assertEqual(loaded[0].received_count, 0)

    def test_v1_cache_becomes_legacy_folder(self) -> None:
        self._write_v1()
        folders = correspondent_cache.load_folders("acct-1")
        self.assertEqual(
            list(folders), [correspondent_cache.LEGACY_FOLDER_KEY]
        )
        self.assertEqual(folders[correspondent_cache.LEGACY_FOLDER_KEY][0].last_seen, 1)

    def test_unknown_cache_version_is_discarded(self) -> None:
        path = correspondent_cache._cache_path("acct-1")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 99, "correspondents": []}), encoding="utf-8"
        )
        self.assertIsNone(correspondent_cache.load("acct-1"))
        self.assertIsNone(correspondent_cache.load_folders("acct-1"))

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
