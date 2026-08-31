# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from post.mail import folder_index_cache


class FolderIndexCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_root = Path(self._tmpdir.name)
        self._patch = patch.object(
            folder_index_cache,
            "_CACHE_ROOT",
            self._cache_root,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_save_and_load_round_trip(self) -> None:
        messages = [
            {
                "uid": "42",
                "subject": "Hello",
                "from": "Alice",
                "flags": {"seen": False},
            }
        ]
        folder_index_cache.save("acct-1", "INBOX", messages, unread=1, total=1)
        loaded = folder_index_cache.load("acct-1", "INBOX")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        loaded_messages, unread, total = loaded
        self.assertEqual(loaded_messages, messages)
        self.assertEqual(unread, 1)
        self.assertEqual(total, 1)

    def test_load_returns_none_for_missing_cache(self) -> None:
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))
        self.assertFalse(folder_index_cache.has_cache("acct-1", "INBOX"))

    def test_has_cache_after_save(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [], unread=0, total=0)
        self.assertTrue(folder_index_cache.has_cache("acct-1", "INBOX"))

    def test_load_rejects_mismatched_folder_name(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [], unread=0, total=0)
        self.assertIsNone(folder_index_cache.load("acct-1", "Sent"))

    def test_invalidate_removes_single_folder(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [], unread=0, total=0)
        folder_index_cache.save("acct-1", "Sent", [], unread=0, total=0)
        folder_index_cache.invalidate("acct-1", "INBOX")
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))
        self.assertIsNotNone(folder_index_cache.load("acct-1", "Sent"))

    def test_invalidate_account_removes_all_folders(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [], unread=0, total=0)
        folder_index_cache.save("acct-1", "Sent", [], unread=0, total=0)
        folder_index_cache.invalidate_account("acct-1")
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))
        self.assertIsNone(folder_index_cache.load("acct-1", "Sent"))

    def test_load_rejects_invalid_json(self) -> None:
        path = folder_index_cache._cache_path("acct-1", "INBOX")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))

    def test_save_uses_atomic_replace(self) -> None:
        messages = [{"uid": "1", "subject": "One"}]
        folder_index_cache.save("acct-1", "Archive/2026", messages, unread=0, total=1)
        loaded = folder_index_cache.load("acct-1", "Archive/2026")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded[0], messages)

        payload_path = folder_index_cache._cache_path("acct-1", "Archive/2026")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["folder_name"], "Archive/2026")

    def test_grow_only_refuses_shrink(self) -> None:
        large = [{"uid": str(i)} for i in range(10)]
        folder_index_cache.save(
            "acct-1", "Archive", large, unread=1, total=28266, grow_only=True
        )
        folder_index_cache.save(
            "acct-1",
            "Archive",
            [{"uid": "1"}],
            unread=0,
            total=1,
            grow_only=True,
        )
        loaded = folder_index_cache.load("acct-1", "Archive")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(len(loaded[0]), 10)
        self.assertEqual(loaded[2], 28266)

    def test_cached_folder_names_lists_saved_folders(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [{"uid": "1"}], unread=0, total=1)
        folder_index_cache.save("acct-1", "Archive", [{"uid": "2"}], unread=0, total=1)
        names = set(folder_index_cache.cached_folder_names("acct-1"))
        self.assertEqual(names, {"INBOX", "Archive"})

    def test_cached_account_uids_lists_saved_accounts(self) -> None:
        folder_index_cache.save("acct-1", "INBOX", [{"uid": "1"}], unread=0, total=1)
        folder_index_cache.save("acct-2", "INBOX", [{"uid": "2"}], unread=0, total=1)
        self.assertEqual(
            set(folder_index_cache.cached_account_uids()),
            {"acct-1", "acct-2"},
        )


if __name__ == "__main__":
    unittest.main()
