# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for dropping leftover account caches after GOA re-add (#366)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from post.mail import correspondent_cache
from post.mail import folder_index_cache
from post.mail import folder_status_cache
from post.mail.account_cache_gc import drop_orphan_account_caches
from post.mail.correspondents import Correspondent
from post.mail.eds import MailService


class DropOrphanAccountCachesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._index_root = root / "folder-index"
        self._status_root = root / "folder-status"
        self._corr_root = root / "correspondents"
        self._patches = [
            patch.object(folder_index_cache, "_CACHE_ROOT", self._index_root),
            patch.object(folder_status_cache, "_CACHE_ROOT", self._status_root),
            patch.object(correspondent_cache, "_CACHE_ROOT", self._corr_root),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()
        self._tmpdir.cleanup()

    def _seed(self, account_uid: str) -> None:
        folder_index_cache.save(
            account_uid, "INBOX", [{"uid": "1"}], unread=0, total=1
        )
        folder_status_cache.observe(
            account_uid, "Archive", 3803, 28177, trusted=True
        )
        correspondent_cache.save(
            account_uid,
            [
                Correspondent(
                    display="Alice <alice@example.com>",
                    email="alice@example.com",
                    name="Alice",
                    last_seen=1,
                )
            ],
        )

    def test_keeps_live_uid_and_drops_missing_uid(self) -> None:
        self._seed("live")
        self._seed("dead")
        dropped = drop_orphan_account_caches({"live"})
        self.assertEqual(dropped, ["dead"])
        self.assertTrue(folder_index_cache.has_cache("live", "INBOX"))
        self.assertIsNotNone(folder_status_cache.load("live", "Archive"))
        self.assertIsNotNone(correspondent_cache.load("live"))
        self.assertFalse(folder_index_cache.has_cache("dead", "INBOX"))
        self.assertIsNone(folder_status_cache.load("dead", "Archive"))
        self.assertIsNone(correspondent_cache.load("dead"))
        self.assertFalse((self._index_root / "dead").exists())
        self.assertFalse((self._status_root / "dead").exists())
        self.assertFalse((self._corr_root / "dead").exists())

    def test_empty_live_set_drops_all_cached_uids(self) -> None:
        self._seed("dead")
        dropped = drop_orphan_account_caches(set())
        self.assertEqual(dropped, ["dead"])
        self.assertFalse((self._index_root / "dead").exists())

    def test_does_not_rename_orphan_onto_live_uid(self) -> None:
        self._seed("dead")
        folder_index_cache.save(
            "live", "INBOX", [{"uid": "new"}], unread=0, total=1
        )
        drop_orphan_account_caches({"live"})
        loaded = folder_index_cache.load("live", "INBOX")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded[0], [{"uid": "new"}])
        self.assertFalse((self._index_root / "dead").exists())


class MailServiceOrphanCacheTests(unittest.TestCase):
    def test_skips_gc_when_list_sources_fails(self) -> None:
        registry = MagicMock()
        registry.list_sources.side_effect = RuntimeError("eds down")
        service = MailService(registry=registry)
        with patch(
            "post.mail.eds.drop_orphan_account_caches"
        ) as drop:
            service._drop_orphan_account_caches()
        drop.assert_not_called()

    def test_skips_gc_when_list_sources_returns_none(self) -> None:
        registry = MagicMock()
        registry.list_sources.return_value = None
        service = MailService(registry=registry)
        with patch(
            "post.mail.eds.drop_orphan_account_caches"
        ) as drop:
            service._drop_orphan_account_caches()
        drop.assert_not_called()

    def test_passes_all_mail_account_uids_not_list_accounts(self) -> None:
        live = MagicMock()
        live.get_uid.return_value = "live-uid"
        disabled = MagicMock()
        disabled.get_uid.return_value = "disabled-uid"
        registry = MagicMock()
        registry.list_sources.return_value = [live, disabled]
        service = MailService(registry=registry)
        with patch(
            "post.mail.eds.drop_orphan_account_caches"
        ) as drop:
            service._drop_orphan_account_caches()
        registry.list_sources.assert_called_once_with("Mail Account")
        drop.assert_called_once_with({"live-uid", "disabled-uid"})


if __name__ == "__main__":
    unittest.main()
