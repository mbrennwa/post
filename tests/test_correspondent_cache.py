# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from post.mail import correspondent_cache
from post.mail.correspondents import Correspondent, aggregate_correspondents


def _person(email: str, *, received: int = 0) -> Correspondent:
    return Correspondent(
        display=email, email=email, name="", last_seen=1, received_count=received
    )


def _hammer_folder(cache_root: str, folder: str, rounds: int) -> None:
    """Child process: repeatedly replace ``folder``'s contribution."""
    correspondent_cache._CACHE_ROOT = Path(cache_root)
    item = _person(f"{folder}@example.com")
    for _ in range(rounds):
        correspondent_cache.update_folder(
            "acct-1", folder, [item], aggregate_correspondents
        )


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

    def test_v1_cache_is_ignored(self) -> None:
        self._write_v1()
        self.assertIsNone(correspondent_cache.load("acct-1"))
        self.assertIsNone(correspondent_cache.load_folders("acct-1"))

    def test_v1_cache_is_replaced_by_first_save(self) -> None:
        self._write_v1()
        bob = Correspondent(display="bob@example.com", email="bob@example.com", name="")
        correspondent_cache.save("acct-1", [bob], folders={"INBOX": [bob]})
        self.assertEqual(
            [item.email for item in correspondent_cache.load("acct-1")],
            ["bob@example.com"],
        )
        self.assertEqual(list(correspondent_cache.load_folders("acct-1")), ["INBOX"])

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

    # --- stamp / update_folder / locking (#547) ---

    def test_stamp_is_none_without_file(self) -> None:
        self.assertIsNone(correspondent_cache.stamp("acct-1"))

    def test_stamp_changes_with_every_write_even_at_equal_size(self) -> None:
        a = _person("a@example.com")
        b = _person("b@example.com")  # same length as a: same file size
        correspondent_cache.save("acct-1", [a])
        first = correspondent_cache.stamp("acct-1")
        correspondent_cache.save("acct-1", [b])
        second = correspondent_cache.stamp("acct-1")
        self.assertIsNotNone(first)
        self.assertNotEqual(first, second)

    def test_load_snapshot_stamp_matches_file(self) -> None:
        correspondent_cache.save("acct-1", [_person("a@example.com")])
        snapshot = correspondent_cache.load_snapshot("acct-1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.stamp, correspondent_cache.stamp("acct-1"))

    def test_save_returns_stamp_and_if_absent_keeps_existing_file(self) -> None:
        a = _person("a@example.com")
        b = _person("b@example.com")
        written = correspondent_cache.save("acct-1", [a], if_absent=True)
        self.assertEqual(written, correspondent_cache.stamp("acct-1"))
        self.assertIsNone(correspondent_cache.save("acct-1", [b], if_absent=True))
        self.assertEqual(correspondent_cache.load("acct-1"), [a])

    def test_update_folder_replaces_one_contribution(self) -> None:
        a = _person("a@example.com", received=2)
        b = _person("b@example.com", received=1)
        correspondent_cache.update_folder("acct-1", "INBOX", [a], aggregate_correspondents)
        correspondent_cache.update_folder("acct-1", "Sent", [b], aggregate_correspondents)
        a2 = _person("a@example.com", received=5)
        merged, folders, file_stamp = correspondent_cache.update_folder(
            "acct-1", "INBOX", [a2], aggregate_correspondents
        )
        self.assertEqual(set(folders), {"INBOX", "Sent"})
        # Replaced, not added: 5 rather than 2 + 5.
        by_email = {item.email: item for item in merged}
        self.assertEqual(by_email["a@example.com"].received_count, 5)
        self.assertEqual(by_email["b@example.com"].received_count, 1)
        self.assertEqual(file_stamp, correspondent_cache.stamp("acct-1"))
        self.assertEqual(correspondent_cache.load("acct-1"), merged)

    def test_update_folder_starts_from_scratch_without_file(self) -> None:
        a = _person("a@example.com")
        merged, folders, file_stamp = correspondent_cache.update_folder(
            "acct-1", "INBOX", [a], aggregate_correspondents
        )
        self.assertEqual(merged, [a])
        self.assertEqual(folders, {"INBOX": [a]})
        self.assertIsNotNone(file_stamp)

    def test_update_folder_without_addresses_writes_nothing(self) -> None:
        merged, _folders, file_stamp = correspondent_cache.update_folder(
            "acct-1", "INBOX", [], aggregate_correspondents
        )
        self.assertEqual(merged, [])
        self.assertIsNone(file_stamp)
        self.assertIsNone(correspondent_cache.stamp("acct-1"))

    def test_update_folder_keeps_other_writers_contribution(self) -> None:
        """A stale ``known`` base must not overwrite what another writer saved."""
        a = _person("a@example.com")
        helper = _person("helper@example.com")
        _merged, folders, stale_stamp = correspondent_cache.update_folder(
            "acct-1", "INBOX", [a], aggregate_correspondents
        )
        # The other process adds a folder; ``folders`` / ``stale_stamp`` are
        # now stale.
        correspondent_cache.update_folder(
            "acct-1", "Sent", [helper], aggregate_correspondents
        )
        merged, folders, _stamp = correspondent_cache.update_folder(
            "acct-1",
            "INBOX",
            [a],
            aggregate_correspondents,
            known=(stale_stamp, folders),
        )
        self.assertEqual(set(folders), {"INBOX", "Sent"})
        self.assertEqual({item.email for item in merged}, {a.email, helper.email})

    def test_update_folder_skips_parsing_when_known_stamp_is_current(self) -> None:
        a = _person("a@example.com")
        b = _person("b@example.com")
        _merged, folders, file_stamp = correspondent_cache.update_folder(
            "acct-1", "INBOX", [a], aggregate_correspondents
        )
        with patch.object(
            correspondent_cache,
            "_read_payload",
            side_effect=AssertionError("file must not be parsed"),
        ):
            merged, folders, _stamp = correspondent_cache.update_folder(
                "acct-1",
                "Sent",
                [b],
                aggregate_correspondents,
                known=(file_stamp, folders),
            )
        self.assertEqual(set(folders), {"INBOX", "Sent"})
        self.assertEqual({item.email for item in merged}, {a.email, b.email})

    def test_update_folder_waits_for_the_cross_process_lock(self) -> None:
        """A writer blocks while another holds the lock, then proceeds."""
        done = threading.Event()

        def write() -> None:
            correspondent_cache.update_folder(
                "acct-1", "INBOX", [_person("a@example.com")], aggregate_correspondents
            )
            done.set()

        with correspondent_cache._locked("acct-1"):
            writer = threading.Thread(target=write)
            writer.start()
            self.assertFalse(done.wait(timeout=0.3))
            self.assertIsNone(correspondent_cache.stamp("acct-1"))
        self.assertTrue(done.wait(timeout=10))
        writer.join(timeout=10)
        self.assertIsNotNone(correspondent_cache.stamp("acct-1"))

    def test_concurrent_writers_do_not_lose_each_others_folders(self) -> None:
        """Two processes updating different folders both survive."""
        context = multiprocessing.get_context("fork")
        root = str(correspondent_cache._CACHE_ROOT)
        workers = [
            context.Process(target=_hammer_folder, args=(root, name, 40))
            for name in ("INBOX", "Sent")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
            self.assertEqual(worker.exitcode, 0)
        folders = correspondent_cache.load_folders("acct-1")
        self.assertEqual(set(folders), {"INBOX", "Sent"})
        self.assertEqual(
            {item.email for item in correspondent_cache.load("acct-1")},
            {"INBOX@example.com", "Sent@example.com"},
        )

    def test_invalidate_without_cache_creates_nothing(self) -> None:
        correspondent_cache.invalidate("acct-1")
        self.assertFalse((Path(self._tmpdir.name) / "acct-1").exists())

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
