# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for grow-only heavy-folder STATUS cache (#208)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from post.mail import folder_status_cache


class FolderStatusCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        patcher = mock.patch.object(
            folder_status_cache,
            "_CACHE_ROOT",
            Path(self._tmpdir.name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_does_not_persist_small_first_observation(self) -> None:
        unread, total = folder_status_cache.observe(
            "acct", "Archive", 53, 530, trusted=True
        )
        self.assertEqual((unread, total), (53, 530))
        self.assertIsNone(folder_status_cache.load("acct", "Archive"))
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Archive", 53, 530),
            (-1, -1),
        )

    def test_persists_large_status_and_never_shrinks_with_summary(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 3803, 28177, trusted=True
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.load("acct", "Archive"),
            (3803, 28177),
        )
        # Local Camel summary / poisoned REFRESH must not overwrite STATUS.
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 53, 530, trusted=True
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Archive", 53, 530),
            (3803, 28177),
        )

    def test_trusted_refresh_may_shrink_still_large_totals(self) -> None:
        folder_status_cache.observe(
            "acct", "Archive", 100, 20000, trusted=True
        )
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 50, 15000, trusted=True
            ),
            (50, 15000),
        )

    def test_untrusted_summary_never_shrinks_large_high_water(self) -> None:
        folder_status_cache.observe(
            "acct", "Archive", 100, 20000, trusted=True
        )
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 50, 15000, trusted=False
            ),
            (100, 20000),
        )

    def test_best_without_cache_returns_input(self) -> None:
        self.assertEqual(
            folder_status_cache.best("acct", "Archive", 1, 2),
            (1, 2),
        )

    def test_resolve_shows_large_folderinfo_before_cache(self) -> None:
        self.assertEqual(
            folder_status_cache.resolve_sidebar(
                "acct", "Archive", 3803, 28177
            ),
            (3803, 28177),
        )


if __name__ == "__main__":
    unittest.main()
