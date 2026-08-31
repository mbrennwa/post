# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for grow-only heavy-folder STATUS cache (#208)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from post.mail import folder_status_cache
from post.mail.graph_folder_counts import graph_well_known_folder_id


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

    def test_trusted_echo_of_local_index_does_not_lock_in(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct",
                "Archive",
                96,
                1170,
                trusted=True,
                local_indexed=1173,
            ),
            (96, 1170),
        )
        self.assertIsNone(folder_status_cache.load("acct", "Archive"))
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Archive", 96, 1170),
            (-1, -1),
        )

    def test_trusted_near_local_index_does_not_lock_in(self) -> None:
        # Growing Camel summary ~1320 must not become "the server total".
        self.assertEqual(
            folder_status_cache.observe(
                "acct",
                "Archive",
                117,
                1320,
                trusted=True,
                local_indexed=1300,
            ),
            (117, 1320),
        )
        self.assertIsNone(folder_status_cache.load("acct", "Archive"))

    def test_untrusted_large_total_does_not_lock_in_as_status(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 79, 1000, trusted=False
            ),
            (79, 1000),
        )
        self.assertIsNone(folder_status_cache.load("acct", "Archive"))
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Archive", 79, 1000),
            (-1, -1),
        )

    def test_graph_sized_total_locks_in(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct",
                "Archive",
                3803,
                28177,
                trusted=True,
                local_indexed=1300,
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.load("acct", "Archive"),
            (3803, 28177),
        )

    def test_scrub_summary_echo_clears_poison(self) -> None:
        folder_status_cache.observe(
            "acct", "Archive", 117, 28177, trusted=True, local_indexed=100
        )
        # Simulate a later poison write by saving directly then scrubbing.
        folder_status_cache.clear("acct", "Archive")
        folder_status_cache._save("acct", "Archive", 117, 1320)
        self.assertEqual(
            folder_status_cache.load("acct", "Archive"),
            (117, 1320),
        )
        folder_status_cache.scrub_if_summary_echo("acct", "Archive", 1300)
        self.assertIsNone(folder_status_cache.load("acct", "Archive"))

    def test_persists_large_status_and_never_shrinks(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 3803, 28177, trusted=True
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 53, 530, trusted=True
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Archive", 79, 1000, trusted=True
            ),
            (3803, 28177),
        )
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Archive", 53, 530),
            (3803, 28177),
        )

    def test_index_caught_up_rejects_unknown_and_echo(self) -> None:
        self.assertFalse(folder_status_cache.index_caught_up(400, -1))
        self.assertFalse(folder_status_cache.index_caught_up(1300, 1320))
        self.assertFalse(folder_status_cache.index_caught_up(400, 28177))
        self.assertTrue(folder_status_cache.index_caught_up(28177, 28177))

    def test_trusted_small_trash_junk_locks_in(self) -> None:
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Spam", 12, 319, trusted=True
            ),
            (12, 319),
        )
        self.assertEqual(
            folder_status_cache.load("acct", "Spam"),
            (12, 319),
        )
        self.assertEqual(
            folder_status_cache.resolve_sidebar("acct", "Spam", -1, -1),
            (12, 319),
        )
        self.assertEqual(
            folder_status_cache.observe(
                "acct", "Trash", 0, 47, trusted=True
            ),
            (0, 47),
        )
        self.assertEqual(folder_status_cache.load("acct", "Trash"), (0, 47))

    def test_scrub_does_not_clear_small_junk_status(self) -> None:
        folder_status_cache.observe(
            "acct", "Junk", 3, 80, trusted=True
        )
        folder_status_cache.scrub_if_summary_echo("acct", "Junk", 80)
        self.assertEqual(
            folder_status_cache.load("acct", "Junk"),
            (3, 80),
        )

    def test_index_caught_up_trash_junk_small_totals(self) -> None:
        self.assertTrue(
            folder_status_cache.index_caught_up(319, 319, "Spam")
        )
        self.assertFalse(
            folder_status_cache.index_caught_up(200, 319, "Spam")
        )
        # Archive still rejects summary-sized catch-up.
        self.assertFalse(
            folder_status_cache.index_caught_up(530, 530, "Archive")
        )

    def test_status_total_is_trusted(self) -> None:
        self.assertTrue(
            folder_status_cache.status_total_is_trusted("Spam", 319)
        )
        self.assertFalse(
            folder_status_cache.status_total_is_trusted("Spam", -1)
        )
        self.assertFalse(
            folder_status_cache.status_total_is_trusted("Archive", 530)
        )
        self.assertTrue(
            folder_status_cache.status_total_is_trusted("Archive", 28177)
        )

    def test_invalidate_account_removes_status_dir(self) -> None:
        folder_status_cache.observe(
            "acct-1", "Archive", 3803, 28177, trusted=True
        )
        folder_status_cache.observe(
            "acct-2", "Archive", 3803, 28177, trusted=True
        )
        self.assertEqual(
            set(folder_status_cache.cached_account_uids()),
            {"acct-1", "acct-2"},
        )
        folder_status_cache.invalidate_account("acct-1")
        self.assertIsNone(folder_status_cache.load("acct-1", "Archive"))
        self.assertEqual(
            folder_status_cache.load("acct-2", "Archive"),
            (3803, 28177),
        )


class GraphFolderCountsTests(unittest.TestCase):
    def test_well_known_ids(self) -> None:
        self.assertEqual(graph_well_known_folder_id("Archive"), "archive")
        self.assertEqual(graph_well_known_folder_id("Junk Email"), "junkemail")
        self.assertEqual(graph_well_known_folder_id("Deleted Items"), "deleteditems")
        self.assertIsNone(graph_well_known_folder_id("Projects/Foo"))


if __name__ == "__main__":
    unittest.main()
