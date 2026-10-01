# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from post.mail import pending_removals
from post.mail.eds import MailService, _FolderMessageIndex
from post.mail.operation_queue import (
    QueuedOperation,
    acquire_flush_lease,
    coalesce_or_enqueue_operation,
    count_queued_operations,
    list_queued_operations,
    release_flush_lease,
)


class PendingRemovalsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir_patch = mock.patch(
            "post.mail.pending_removals.pending_removals_dir",
            return_value=self._tmpdir.name,
        )
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()
        self._tmpdir.cleanup()

    def test_note_clear_and_reload(self) -> None:
        pending_removals.note_pending_uids("acct-1", "INBOX", ["a", "b"])
        self.assertEqual(
            pending_removals.load_pending_uids("acct-1", "INBOX"),
            {"a", "b"},
        )
        remaining = pending_removals.clear_pending_uids("acct-1", "INBOX", ["a"])
        self.assertEqual(remaining, {"b"})
        self.assertEqual(
            pending_removals.load_all_pending_removals(),
            {("acct-1", "INBOX"): {"b"}},
        )
        pending_removals.clear_pending_uids("acct-1", "INBOX", ["b"])
        self.assertEqual(pending_removals.load_all_pending_removals(), {})


class FlushLeaseCoalesceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir_patch = mock.patch(
            "post.mail.operation_queue.operations_dir",
            return_value=self._tmpdir.name,
        )
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()
        self._tmpdir.cleanup()

    def test_coalesce_skips_disk_flush_lease(self) -> None:
        first = coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["1"],
            )
        )
        acquire_flush_lease(first)
        try:
            second = coalesce_or_enqueue_operation(
                QueuedOperation(
                    op_type="archive",
                    account_uid="acct-1",
                    folder_name="INBOX",
                    message_uids=["2"],
                )
            )
            self.assertNotEqual(first, second)
            self.assertEqual(count_queued_operations(), 2)
        finally:
            release_flush_lease(first)


class PendingRemovalMailServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pending_dir = tempfile.TemporaryDirectory()
        self._ops_dir = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch(
                "post.mail.pending_removals.pending_removals_dir",
                return_value=self._pending_dir.name,
            ),
            mock.patch(
                "post.mail.operation_queue.operations_dir",
                return_value=self._ops_dir.name,
            ),
        ]
        for patch in self._patches:
            patch.start()
        self.service = MailService.__new__(MailService)
        self.service._pending_local_removals = {}
        self.service._folder_indexes = {}
        self.service._lock = __import__("threading").RLock()
        self.service._correspondent_indexes = {}

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._pending_dir.cleanup()
        self._ops_dir.cleanup()

    def test_note_persists_and_filter_hides(self) -> None:
        self.service._note_pending_local_removals("acct-1", "INBOX", ["gone"])
        self.assertTrue(
            self.service.folder_has_pending_local_removals("acct-1", "INBOX")
        )
        messages = [
            {"uid": "gone", "subject": "x"},
            {"uid": "keep", "subject": "y"},
        ]
        filtered = self.service._filter_pending_local_removals(
            "acct-1", "INBOX", messages
        )
        self.assertEqual([m["uid"] for m in filtered], ["keep"])
        # Survive RAM clear via disk reload (#503).
        self.service._pending_local_removals = {}
        self.service._reload_pending_local_removals_from_disk()
        self.assertEqual(
            self.service._pending_local_removals[("acct-1", "INBOX")],
            {"gone"},
        )

    def test_store_folder_index_strips_pending(self) -> None:
        self.service._note_pending_local_removals("acct-1", "INBOX", ["gone"])
        with mock.patch.object(
            self.service, "_merge_correspondents_from_folder"
        ):
            stored = self.service._store_folder_index(
                "acct-1",
                "INBOX",
                _FolderMessageIndex(
                    messages=[
                        {"uid": "gone", "flags": {"seen": True}},
                        {"uid": "keep", "flags": {"seen": True}},
                    ],
                    unread=0,
                    total=2,
                ),
            )
        self.assertEqual([m["uid"] for m in stored.messages], ["keep"])
        self.assertEqual(stored.total, 1)

    def test_get_folder_messages_filters_helper_result(self) -> None:
        self.service._note_pending_local_removals("acct-1", "INBOX", ["gone"])
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch.object(
                self.service,
                "_camel_helper_call",
                return_value=(
                    [
                        {"uid": "gone"},
                        {"uid": "keep"},
                    ],
                    0,
                    2,
                    "server",
                ),
            ),
        ):
            messages, _unread, _total, _source = self.service.get_folder_messages(
                "acct-1", "INBOX", sync=True
            )
        self.assertEqual([m["uid"] for m in messages], ["keep"])

    def test_partial_flush_rewrites_remaining_uids(self) -> None:
        queue_id = coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["a", "b"],
            )
        )
        self.service._note_pending_local_removals("acct-1", "INBOX", ["a", "b"])
        self.service._flushing_operation_queue = False
        self.service._flushing_queue_ids = set()

        def _health(_uid: str) -> str:
            return "ok"

        self.service.get_account_connect_health = _health  # type: ignore[method-assign]
        with mock.patch.object(
            self.service,
            "_execute_queued_operation_unlocked",
            return_value={"moved_uids": ["a"]},
        ):
            flushed = self.service._flush_operation_queue_unlocked("acct-1")
        self.assertEqual(flushed, 0)
        items = list_queued_operations()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], queue_id)
        self.assertEqual(items[0][1].message_uids, ["b"])
        self.assertEqual(
            pending_removals.load_pending_uids("acct-1", "INBOX"),
            {"b"},
        )

    def test_flush_absent_uids_soft_succeeds(self) -> None:
        """Empty Camel match during flush must clear the op, not ERROR-loop (#503)."""
        coalesce_or_enqueue_operation(
            QueuedOperation(
                op_type="move_to_trash",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["gone"],
            )
        )
        self.service._note_pending_local_removals("acct-1", "INBOX", ["gone"])
        self.service._flushing_queue_ids = set()
        self.service.get_account_connect_health = (  # type: ignore[method-assign]
            lambda _uid: "ok"
        )
        with mock.patch.object(
            self.service,
            "_execute_queued_operation_unlocked",
            return_value={"moved_uids": ["gone"]},
        ):
            flushed = self.service._flush_operation_queue_unlocked("acct-1")
        self.assertEqual(flushed, 1)
        self.assertEqual(list_queued_operations(), [])
        self.assertEqual(pending_removals.load_pending_uids("acct-1", "INBOX"), set())

    def test_transfer_during_flush_treats_empty_match_as_done(self) -> None:
        self.service._flushing_operation_queue = True
        source = mock.Mock()
        source.get_full_name.return_value = "INBOX"
        source.get_message_count.return_value = 0
        dest = mock.Mock()
        dest.get_full_name.return_value = "Trash"
        with (
            mock.patch.object(
                self.service, "_open_folder_unlocked", return_value=source
            ),
            mock.patch.object(
                self.service, "_transfer_uids_in_folder", return_value=[]
            ),
            mock.patch.object(
                self.service, "_allow_online_store_unlocked", return_value=True
            ),
            mock.patch("post.mail.eds.folder_get_unread_count", return_value=0),
            mock.patch.object(self.service, "_remove_messages_from_cache"),
        ):
            result = self.service._transfer_messages_unlocked(
                "acct-1",
                "INBOX",
                ["gone"],
                dest,
                op_type="move_to_trash",
            )
        self.assertEqual(result["moved_uids"], ["gone"])



if __name__ == "__main__":
    unittest.main()
