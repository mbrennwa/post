# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from post.mail.operation_queue import (
    QueuedOperation,
    count_queued_operations,
    enqueue_operation,
    list_queued_operations,
    offline_queue_status_text,
    operations_dir,
    remove_queued_operation,
)


class OperationQueueTests(unittest.TestCase):
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

    def test_enqueue_and_list(self) -> None:
        queue_id = enqueue_operation(
            QueuedOperation(
                op_type="move_to_trash",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["1", "2"],
            )
        )
        self.assertTrue(queue_id)
        items = list_queued_operations()
        self.assertEqual(len(items), 1)
        loaded_id, loaded = items[0]
        self.assertEqual(loaded_id, queue_id)
        self.assertEqual(loaded.op_type, "move_to_trash")
        self.assertEqual(loaded.message_uids, ["1", "2"])
        self.assertEqual(count_queued_operations(), 1)

    def test_remove_operation(self) -> None:
        queue_id = enqueue_operation(
            QueuedOperation(
                op_type="set_seen",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["1"],
                seen=True,
            )
        )
        remove_queued_operation(queue_id)
        self.assertEqual(list_queued_operations(), [])
        self.assertEqual(count_queued_operations(), 0)

    def test_offline_queue_status_text(self) -> None:
        self.assertEqual(
            offline_queue_status_text(
                send_queued_count=0,
                operation_queued_count=0,
            ),
            "Offline",
        )
        self.assertEqual(
            offline_queue_status_text(
                send_queued_count=1,
                operation_queued_count=2,
            ),
            "Offline · 1 message queued · 2 actions queued",
        )

    def test_persists_json_payload(self) -> None:
        enqueue_operation(
            QueuedOperation(
                op_type="archive",
                account_uid="acct-1",
                folder_name="INBOX",
                message_uids=["9"],
            )
        )
        path = os.path.join(self._tmpdir.name, os.listdir(self._tmpdir.name)[0])
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["op_type"], "archive")
        self.assertEqual(payload["message_uids"], ["9"])
