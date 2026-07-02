# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel folder search integration."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio

from post.mail.eds import MailService, _FolderMessageIndex
from post.mail.search import MessageSearchQuery, SearchTerm


class FolderSearchUidTests(unittest.TestCase):
    def test_empty_scope_skips_search(self) -> None:
        folder = mock.Mock()

        with mock.patch("post.mail.eds.folder_search_uids") as folder_search_uids:
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "missing"))',
                [],
            )

        self.assertEqual(result, set())
        folder_search_uids.assert_not_called()

    def test_cancelled_cancellable_skips_search(self) -> None:
        folder = mock.Mock()
        cancellable = Gio.Cancellable()
        cancellable.cancel()

        with mock.patch("post.mail.eds.folder_search_uids") as folder_search_uids:
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                ["uid-1"],
                cancellable,
            )

        self.assertEqual(result, set())
        folder_search_uids.assert_not_called()

    def test_non_empty_matches_return_uid_set(self) -> None:
        folder = mock.Mock()
        index_uids = ["uid-1", "uid-2", "uid-3"]

        with mock.patch(
            "post.mail.eds.folder_search_uids",
            return_value=["uid-1", "uid-2"],
        ) as folder_search_uids:
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                index_uids,
            )

        self.assertEqual(result, {"uid-1", "uid-2"})
        folder_search_uids.assert_called_once_with(
            folder,
            '(match-all (header-contains "Subject" "invoice"))',
            index_uids,
            cancellable=None,
        )

    def test_cancel_folder_search_clears_active_cancellable(self) -> None:
        service = MailService(registry=mock.Mock())
        cancellable = Gio.Cancellable()
        service._folder_search_cancellable = cancellable

        service.cancel_folder_search()

        self.assertTrue(cancellable.is_cancelled())
        self.assertIsNone(service._folder_search_cancellable)


class SearchFolderMessagesTests(unittest.TestCase):
    def _service_with_index(
        self, messages: list[dict]
    ) -> tuple[MailService, mock.Mock]:
        service = MailService(registry=mock.Mock())
        index = _FolderMessageIndex(
            messages=messages,
            unread=sum(
                1
                for message in messages
                if not (message.get("flags") or {}).get("seen", False)
            ),
            total=len(messages),
        )
        service._folder_indexes[("acct-1", "INBOX")] = index
        folder = mock.Mock()
        return service, folder

    def test_header_query_filters_index_without_body_loader(self) -> None:
        messages = [
            {
                "uid": "1",
                "subject": "Invoice due",
                "from": "a@b.c",
                "flags": {"seen": True},
            },
            {
                "uid": "2",
                "subject": "Hello",
                "from": "a@b.c",
                "flags": {"seen": True},
            },
        ]
        service, folder = self._service_with_index(messages)
        query = MessageSearchQuery(terms=(SearchTerm(field="subject", value="Invoice"),))

        with (
            mock.patch.object(service, "_require_folder_unlocked", return_value=folder),
            mock.patch("post.mail.eds.filter_messages_by_query") as filter_query,
        ):
            filter_query.return_value = [messages[0]]
            matched, unread, total, source = service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
            )

        filter_query.assert_called_once()
        _args, kwargs = filter_query.call_args
        self.assertIsNone(kwargs.get("body_text_for_uid"))
        self.assertEqual([message["uid"] for message in matched], ["1"])
        self.assertEqual(total, 1)
        self.assertEqual(unread, 0)
        self.assertEqual(source, "memory")

    def test_forwards_on_matches_to_filter(self) -> None:
        messages = [
            {
                "uid": "1",
                "subject": "Invoice due",
                "from": "a@b.c",
                "flags": {"seen": True},
            },
        ]
        service, folder = self._service_with_index(messages)
        query = MessageSearchQuery(terms=(SearchTerm(field="subject", value="Invoice"),))
        on_matches = mock.Mock()

        with (
            mock.patch.object(service, "_require_folder_unlocked", return_value=folder),
            mock.patch("post.mail.eds.filter_messages_by_query") as filter_query,
        ):
            filter_query.return_value = [messages[0]]
            service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
                on_matches=on_matches,
            )

        _args, kwargs = filter_query.call_args
        self.assertIs(kwargs.get("on_matches"), on_matches)

    def test_get_folder_index_snapshot_returns_memory_index(self) -> None:
        messages = [
            {"uid": "1", "subject": "Hello", "flags": {"seen": True}},
        ]
        service, _folder = self._service_with_index(messages)
        snapshot = service.get_folder_index_snapshot("acct-1", "INBOX")
        assert snapshot is not None
        loaded_messages, unread, total = snapshot
        self.assertEqual([message["uid"] for message in loaded_messages], ["1"])
        self.assertEqual(unread, 0)
        self.assertEqual(total, 1)

    def test_get_folder_index_snapshot_returns_none_when_missing(self) -> None:
        service = MailService(registry=mock.Mock())
        self.assertIsNone(service.get_folder_index_snapshot("acct-1", "INBOX"))

    def test_text_query_loads_body_text_for_candidates(self) -> None:
        messages = [
            {"uid": "1", "subject": "Hello", "flags": {"seen": True}},
            {"uid": "2", "subject": "Hello", "flags": {"seen": True}},
        ]
        service, folder = self._service_with_index(messages)
        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="invoice"),))

        with (
            mock.patch.object(service, "_require_folder_unlocked", return_value=folder),
            mock.patch(
                "post.mail.eds.filter_messages_by_query",
                return_value=[messages[1]],
            ) as filter_query,
        ):
            matched, _unread, total, _source = service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
            )

        filter_query.assert_called_once()
        _args, kwargs = filter_query.call_args
        self.assertEqual(_args[0], messages)
        self.assertIsNotNone(kwargs.get("body_text_for_uid"))
        self.assertEqual([message["uid"] for message in matched], ["2"])
        self.assertEqual(total, 1)


class SearchAllMessagesTests(unittest.TestCase):
    def test_merges_and_annotates_matches_from_multiple_folders(self) -> None:
        inbox_messages = [
            {
                "uid": "1",
                "subject": "Invoice due",
                "from": "a@b.c",
                "sort_date": 200,
                "flags": {"seen": True},
            },
        ]
        sent_messages = [
            {
                "uid": "1",
                "subject": "Invoice copy",
                "from": "a@b.c",
                "sort_date": 100,
                "flags": {"seen": True},
            },
        ]
        service = MailService(registry=mock.Mock())
        service._folder_indexes[("acct-1", "INBOX")] = _FolderMessageIndex(
            messages=inbox_messages,
            unread=0,
            total=1,
        )
        service._folder_indexes[("acct-1", "Sent")] = _FolderMessageIndex(
            messages=sent_messages,
            unread=0,
            total=1,
        )
        query = MessageSearchQuery(terms=(SearchTerm(field="subject", value="Invoice"),))

        with (
            mock.patch.object(
                service,
                "list_accounts",
                return_value=[mock.Mock(uid="acct-1")],
            ),
            mock.patch.object(
                service,
                "_ordered_searchable_folders_unlocked",
                return_value=[
                    {"full_name": "INBOX", "display_name": "Inbox"},
                    {"full_name": "Sent", "display_name": "Sent"},
                ],
            ),
            mock.patch.object(
                service,
                "_search_single_folder_index_unlocked",
                side_effect=[
                    (inbox_messages, 0, "memory"),
                    (sent_messages, 0, "memory"),
                ],
            ),
        ):
            matched, unread, total, source = service._search_all_messages_unlocked(
                query
            )

        self.assertEqual(unread, 0)
        self.assertEqual(total, 2)
        self.assertEqual(source, "memory")
        self.assertEqual(len(matched), 2)
        self.assertEqual(matched[0]["_search_folder"], "INBOX")
        self.assertEqual(matched[1]["_search_folder"], "Sent")
        self.assertNotEqual(matched[0]["_search_row_key"], matched[1]["_search_row_key"])

    def test_skips_virtual_folders_via_ordered_searchable_folders(self) -> None:
        service = MailService(registry=mock.Mock())
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 0},
            {
                "full_name": ".#evolution/Junk",
                "display_name": "Junk",
                "flags": 0,
            },
        ]
        with mock.patch.object(service, "_list_folders_unlocked", return_value=folders):
            ordered = service._ordered_searchable_folders_unlocked("acct-1")
        self.assertEqual([folder["full_name"] for folder in ordered], ["INBOX"])

    def test_cancelled_search_returns_empty(self) -> None:
        service = MailService(registry=mock.Mock())
        query = MessageSearchQuery(terms=(SearchTerm(field="subject", value="x"),))
        cancellable = Gio.Cancellable()
        cancellable.cancel()

        with (
            mock.patch.object(
                service, "_begin_folder_search_unlocked", return_value=cancellable
            ),
            mock.patch.object(service, "list_accounts", return_value=[]),
        ):
            matched, unread, total, source = service._search_all_messages_unlocked(
                query
            )

        self.assertEqual(matched, [])
        self.assertEqual(unread, 0)
        self.assertEqual(total, 0)
        self.assertEqual(source, "memory")


class PreemptBackgroundWorkTests(unittest.TestCase):
    def test_does_not_cancel_folder_list(self) -> None:
        service = MailService(registry=mock.Mock())
        offline_sync = mock.Mock()
        service._offline_sync = offline_sync

        with mock.patch.object(service, "cancel_folder_list") as cancel_folder_list:
            with mock.patch.object(service, "cancel_folder_search") as cancel_search:
                service._preempt_background_work()

        cancel_search.assert_called_once_with()
        cancel_folder_list.assert_not_called()
        offline_sync.cancel_all.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
