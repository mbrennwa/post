# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from post.mail.compose import ComposeAttachment
from post.mail.draft_queue import (
    QueuedDraft,
    enqueue_draft,
    is_queued_draft_id,
    list_queued_drafts,
    remove_queued_draft,
)
from post.mail.eds import MailService
from post.mail.operation_queue import offline_queue_status_text


class DraftQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir_patch = mock.patch(
            "post.mail.draft_queue.draft_queue_dir",
            return_value=self._tmpdir.name,
        )
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()
        self._tmpdir.cleanup()

    def test_enqueue_and_detect(self) -> None:
        queue_id = enqueue_draft(
            QueuedDraft(
                account_uid="acct-1",
                drafts_folder_name="Drafts",
                to=["bob@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
            )
        )
        self.assertTrue(is_queued_draft_id(queue_id))
        self.assertEqual(len(list_queued_drafts()), 1)
        remove_queued_draft(queue_id)
        self.assertFalse(is_queued_draft_id(queue_id))

    def test_offline_status_includes_drafts(self) -> None:
        self.assertIn(
            "1 draft queued",
            offline_queue_status_text(
                send_queued_count=0,
                operation_queued_count=0,
                draft_queued_count=1,
            ),
        )


class SaveDraftOfflineQueueTests(unittest.TestCase):
    def test_save_draft_queues_when_offline(self) -> None:
        service = MailService(registry=mock.Mock())
        service._network_available = False
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.email = "user@example.com"
        service.get_account = mock.Mock(return_value=account)
        service._drafts_folder_name_unlocked = mock.Mock(return_value="Drafts")

        with (
            mock.patch(
                "post.mail.eds.build_draft_mime_message",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                service,
                "_queue_draft_unlocked",
                return_value=("Drafts", "draft-queue-id"),
            ) as queue_draft,
        ):
            folder_name, uid = service._save_draft_unlocked(
                "acct-1",
                to=["bob@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
                body_html=None,
                in_reply_to=None,
                references=None,
                existing_uid=None,
                drafts_folder_name=None,
            )

        queue_draft.assert_called_once()
        self.assertEqual((folder_name, uid), ("Drafts", "draft-queue-id"))

    def test_append_failure_queues_draft(self) -> None:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        service = MailService(registry=mock.Mock())
        service._network_available = True
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.email = "user@example.com"
        service.get_account = mock.Mock(return_value=account)
        service._drafts_folder_name_unlocked = mock.Mock(return_value="Drafts")
        service._append_draft_unlocked = mock.Mock(
            side_effect=RuntimeError(
                "Could not save draft: You must be working online to complete this operation"
            )
        )

        with (
            mock.patch(
                "post.mail.eds.build_draft_mime_message",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                service,
                "_queue_draft_unlocked",
                return_value=("Drafts", "draft-queue-id"),
            ) as queue_draft,
        ):
            folder_name, uid = service._save_draft_unlocked(
                "acct-1",
                to=None,
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
                body_html=None,
                in_reply_to=None,
                references=None,
                existing_uid=None,
                drafts_folder_name="Drafts",
            )

        queue_draft.assert_called_once()
        self.assertEqual(uid, "draft-queue-id")

    def test_cancelled_append_queues_draft(self) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        service = MailService(registry=mock.Mock())
        service._network_available = True
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.email = "user@example.com"
        service.get_account = mock.Mock(return_value=account)
        service._drafts_folder_name_unlocked = mock.Mock(return_value="Drafts")

        cancellable = Gio.Cancellable()
        cancellable.cancel()

        def append_fail(*_args, **_kwargs):
            raise RuntimeError("Could not save draft: Operation was cancelled")

        service._append_draft_unlocked = mock.Mock(side_effect=append_fail)

        with (
            mock.patch(
                "post.mail.eds.build_draft_mime_message",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                service,
                "_queue_draft_unlocked",
                return_value=("Drafts", "draft-queue-id"),
            ) as queue_draft,
            mock.patch("post.mail.eds.is_network_unavailable_error", return_value=True),
        ):
            folder_name, uid = service._save_draft_unlocked(
                "acct-1",
                to=None,
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
                body_html=None,
                in_reply_to=None,
                references=None,
                existing_uid=None,
                drafts_folder_name="Drafts",
                cancellable=cancellable,
            )

        queue_draft.assert_called_once()
        self.assertEqual((folder_name, uid), ("Drafts", "draft-queue-id"))


class CorrespondentsCacheOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        from post.mail import correspondent_cache, folder_index_cache

        self._tmpdir = tempfile.TemporaryDirectory()
        root = self._tmpdir.name
        self._folder_patch = mock.patch.object(
            folder_index_cache, "_CACHE_ROOT", Path(root) / "folder-index"
        )
        self._corr_patch = mock.patch.object(
            correspondent_cache,
            "_CACHE_ROOT",
            Path(root) / "correspondents",
        )
        self._folder_patch.start()
        self._corr_patch.start()

    def tearDown(self) -> None:
        self._corr_patch.stop()
        self._folder_patch.stop()
        self._tmpdir.cleanup()

    def _service(self) -> MailService:
        service = MailService(registry=mock.Mock())
        service._build_folder_index_unlocked = mock.Mock(
            side_effect=AssertionError("must not open folders for correspondents")
        )
        service._get_store_unlocked = mock.Mock(
            side_effect=AssertionError("must not connect for correspondents")
        )
        return service

    def test_correspondents_without_folder_cache_are_empty(self) -> None:
        service = self._service()
        service._folder_tree_cache = {}

        result = service._build_correspondents_index_unlocked("acct-1")
        self.assertEqual(result, [])

    def test_empty_harvest_is_not_cached(self) -> None:
        service = self._service()
        result = service._get_correspondents_unlocked("acct-1")
        self.assertEqual(result, [])
        self.assertNotIn("acct-1", service._correspondent_indexes)

    def test_correspondents_use_memory_folder_index(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._folder_tree_cache = {
            "acct-1": [
                {
                    "full_name": "INBOX",
                    "display_name": "Inbox",
                    "flags": 1024,
                }
            ]
        }
        service._folder_indexes = {
            ("acct-1", "INBOX"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Alice <alice@example.com>",
                        "sort_date": 100,
                    }
                ],
                unread=0,
                total=1,
            )
        }

        result = service._build_correspondents_index_unlocked("acct-1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].email, "alice@example.com")

    def test_correspondents_include_own_from_address(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._folder_tree_cache = {
            "acct-1": [
                {
                    "full_name": "Sent",
                    "display_name": "Sent",
                    "flags": 4096,
                }
            ]
        }
        service._folder_indexes = {
            ("acct-1", "Sent"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Me <me@example.com>",
                        "to": "Alice <alice@example.com>",
                        "sort_date": 100,
                    }
                ],
                unread=0,
                total=1,
            )
        }

        result = service._build_correspondents_index_unlocked("acct-1")
        emails = {item.email for item in result}
        self.assertEqual(emails, {"me@example.com", "alice@example.com"})

    def test_correspondents_include_archive(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._folder_indexes = {
            ("acct-1", "Archive"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "9",
                        "from": "Old <old@example.com>",
                        "sort_date": 1,
                    }
                ],
                unread=0,
                total=1,
            )
        }
        result = service._build_correspondents_index_unlocked("acct-1")
        self.assertEqual({item.email for item in result}, {"old@example.com"})

    def test_correspondents_skip_junk(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._folder_indexes = {
            ("acct-1", "Junk"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Spam <spam@example.com>",
                        "sort_date": 1,
                    }
                ],
                unread=0,
                total=1,
            ),
            ("acct-1", "INBOX"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "2",
                        "from": "Alice <alice@example.com>",
                        "sort_date": 2,
                    }
                ],
                unread=0,
                total=1,
            ),
        }
        result = service._build_correspondents_index_unlocked("acct-1")
        self.assertEqual({item.email for item in result}, {"alice@example.com"})

    def test_empty_cache_then_index_becomes_visible(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        self.assertEqual(service._get_correspondents_unlocked("acct-1"), [])
        self.assertNotIn("acct-1", service._correspondent_indexes)

        service._store_folder_index(
            "acct-1",
            "INBOX",
            _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Alice <alice@example.com>",
                        "sort_date": 10,
                    }
                ],
                unread=0,
                total=1,
            ),
        )
        result = service._get_correspondents_unlocked("acct-1")
        self.assertEqual({item.email for item in result}, {"alice@example.com"})
        self.assertIn("acct-1", service._correspondent_indexes)

    def test_incremental_merge_after_bootstrap(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._store_folder_index(
            "acct-1",
            "INBOX",
            _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Alice <alice@example.com>",
                        "sort_date": 10,
                    }
                ],
                unread=0,
                total=1,
            ),
        )
        first = service._get_correspondents_unlocked("acct-1")
        self.assertEqual({item.email for item in first}, {"alice@example.com"})

        service._store_folder_index(
            "acct-1",
            "Archive",
            _FolderMessageIndex(
                messages=[
                    {
                        "uid": "2",
                        "from": "Bob <bob@example.com>",
                        "sort_date": 20,
                    }
                ],
                unread=0,
                total=1,
            ),
        )
        cached = service.get_correspondents_cached("acct-1")
        self.assertEqual(
            {item.email for item in cached},
            {"alice@example.com", "bob@example.com"},
        )

    def test_more_than_five_hundred_unique_addresses(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        messages = [
            {
                "uid": "old",
                "from": "Old <old@example.com>",
                "sort_date": 1,
            }
        ]
        messages.extend(
            {
                "uid": str(i),
                "from": f"User {i} <user{i}@example.com>",
                "sort_date": i + 10,
            }
            for i in range(500)
        )
        service._folder_indexes = {
            ("acct-1", "INBOX"): _FolderMessageIndex(
                messages=messages, unread=0, total=len(messages)
            )
        }
        result = service._build_correspondents_index_unlocked("acct-1")
        emails = {item.email for item in result}
        self.assertGreater(len(emails), 500)
        self.assertIn("old@example.com", emails)

    def test_accounts_are_isolated(self) -> None:
        from post.mail.eds import _FolderMessageIndex

        service = self._service()
        service._folder_indexes = {
            ("acct-1", "INBOX"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Alice <alice@example.com>",
                        "sort_date": 1,
                    }
                ],
                unread=0,
                total=1,
            ),
            ("acct-2", "INBOX"): _FolderMessageIndex(
                messages=[
                    {
                        "uid": "1",
                        "from": "Bob <bob@example.com>",
                        "sort_date": 1,
                    }
                ],
                unread=0,
                total=1,
            ),
        }
        one = service._build_correspondents_index_unlocked("acct-1")
        two = service._build_correspondents_index_unlocked("acct-2")
        self.assertEqual({item.email for item in one}, {"alice@example.com"})
        self.assertEqual({item.email for item in two}, {"bob@example.com"})

    def test_disk_cache_skips_folder_harvest(self) -> None:
        from post.mail import correspondent_cache
        from post.mail.correspondents import Correspondent

        service = self._service()
        correspondent_cache.save(
            "acct-1",
            [
                Correspondent(
                    display="Alice <alice@example.com>",
                    email="alice@example.com",
                    name="Alice",
                    last_seen=10,
                )
            ],
        )
        service._build_correspondents_index_unlocked = mock.Mock(
            side_effect=AssertionError(
                "must not harvest folders when correspondent disk cache exists"
            )
        )
        result = service._get_correspondents_unlocked("acct-1")
        self.assertEqual({item.email for item in result}, {"alice@example.com"})
        self.assertEqual(
            {item.email for item in service.get_correspondents_cached("acct-1")},
            {"alice@example.com"},
        )
