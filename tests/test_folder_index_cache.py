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

    def _plant(self, account_uid: str, folder_name: str, payload: dict) -> Path:
        path = folder_index_cache._cache_path(account_uid, folder_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_save_does_not_write_a_mailbox(self) -> None:
        folder_index_cache.save(
            "acct-1",
            "INBOX",
            [{"uid": "42", "subject": "Hello"}],
            unread=1,
            total=1,
        )
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))
        self.assertFalse(folder_index_cache.has_cache("acct-1", "INBOX"))
        self.assertEqual(folder_index_cache.cached_folder_names("acct-1"), [])
        self.assertEqual(folder_index_cache.cached_account_uids(), [])
        self.assertEqual(list(self._cache_root.iterdir()), [])

    def test_load_ignores_a_leftover_file(self) -> None:
        path = self._plant(
            "acct-1",
            "INBOX",
            {"folder_name": "INBOX", "messages": [{"uid": "42"}]},
        )
        self.assertTrue(path.is_file())
        self.assertIsNone(folder_index_cache.load("acct-1", "INBOX"))
        self.assertFalse(folder_index_cache.has_cache("acct-1", "INBOX"))

    def test_invalidate_removes_a_leftover_file(self) -> None:
        inbox = self._plant("acct-1", "INBOX", {"messages": []})
        sent = self._plant("acct-1", "Sent", {"messages": []})
        folder_index_cache.invalidate("acct-1", "INBOX")
        self.assertFalse(inbox.exists())
        self.assertTrue(sent.exists())

    def test_invalidate_account_removes_leftover_files(self) -> None:
        self._plant("acct-1", "INBOX", {"messages": []})
        self._plant("acct-1", "Sent", {"messages": []})
        folder_index_cache.invalidate_account("acct-1")
        self.assertFalse((self._cache_root / "acct-1").exists())


if __name__ == "__main__":
    unittest.main()
