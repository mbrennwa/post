# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel folder search integration."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest import mock

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio

from post.mail.eds import MailService, _FolderMessageIndex
from post.mail.search import MessageSearchQuery, SearchTerm, parse_search_query


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

    def _patch_summary_search(self, service, folder, messages, hits):
        by_uid = {message["uid"]: message for message in messages}

        def info_to_dict(_info, uid=None, backend=None):
            del backend
            return dict(by_uid[str(uid)])

        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(service, "_open_folder_unlocked", return_value=folder)
        )
        stack.enter_context(
            mock.patch("post.mail.eds.folder_get_uids", return_value=list(by_uid))
        )
        stack.enter_context(
            mock.patch("post.mail.eds.folder_get_unread_count", return_value=0)
        )
        stack.enter_context(
            mock.patch(
                "post.mail.eds.folder_get_message_info",
                side_effect=lambda _folder, uid: by_uid.get(str(uid)),
            )
        )
        stack.enter_context(
            mock.patch(
                "post.mail.eds.message_info_to_dict", side_effect=info_to_dict
            )
        )
        stack.enter_context(
            mock.patch.object(
                service,
                "_folder_search_uids_unlocked",
                return_value=set(hits),
            )
        )
        return stack

    def test_header_query_returns_summary_hits(self) -> None:
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

        with self._patch_summary_search(service, folder, messages, ["1"]):
            matched, unread, total, source = service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
            )

        self.assertEqual([message["uid"] for message in matched], ["1"])
        self.assertEqual(total, 1)
        self.assertEqual(unread, 0)
        self.assertEqual(source, "local")

    def test_forwards_summary_hits_to_on_matches(self) -> None:
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

        with self._patch_summary_search(service, folder, messages, ["1"]):
            service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
                on_matches=on_matches,
            )

        on_matches.assert_called_once()
        batch = on_matches.call_args.args[0]
        self.assertEqual([message["uid"] for message in batch], ["1"])

    def test_header_search_ignores_folder_index_disk_rows(self) -> None:
        ram = [
            {
                "uid": "1",
                "subject": "Hello",
                "from": "other@example.com",
                "flags": {"seen": True},
            },
        ]
        service, folder = self._service_with_index(ram)
        query = parse_search_query("from: sender")
        assert query is not None

        with self._patch_summary_search(service, folder, ram, []):
            matched, _unread, total, source = (
                service._search_folder_messages_unlocked(
                    "acct-1",
                    "INBOX",
                    query,
                    sync=False,
                )
            )

        self.assertEqual(matched, [])
        self.assertEqual(total, 0)
        self.assertEqual(source, "local")
        self.assertEqual(
            [message["uid"] for message in service._folder_indexes[("acct-1", "INBOX")].messages],
            ["1"],
        )

    def test_get_folder_index_for_search_does_not_store_union(self) -> None:
        ram = [{"uid": "1", "from": "a@b.c", "flags": {"seen": True}}]
        disk = [
            {"uid": "1", "from": "a@b.c", "flags": {"seen": True}},
            {"uid": "2", "from": "b@c.d", "flags": {"seen": True}},
        ]
        service, _folder = self._service_with_index(ram)

        with mock.patch(
            "post.mail.eds.folder_index_cache.load",
            return_value=(disk, 0, 2),
        ):
            snapshot = service.get_folder_index_for_search("acct-1", "INBOX")

        assert snapshot is not None
        messages, _unread, total = snapshot
        self.assertEqual({message["uid"] for message in messages}, {"1", "2"})
        self.assertEqual(total, 2)
        self.assertEqual(
            [message["uid"] for message in service._folder_indexes[("acct-1", "INBOX")].messages],
            ["1"],
        )

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

    def test_text_query_uses_camel_search_hits(self) -> None:
        messages = [
            {"uid": "1", "subject": "Hello", "flags": {"seen": True}},
            {"uid": "2", "subject": "Hello", "flags": {"seen": True}},
        ]
        service, folder = self._service_with_index(messages)
        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="invoice"),))

        with self._patch_summary_search(service, folder, messages, ["2"]):
            matched, _unread, total, _source = service._search_folder_messages_unlocked(
                "acct-1",
                "INBOX",
                query,
                sync=False,
            )

        self.assertEqual([message["uid"] for message in matched], ["2"])
        self.assertEqual(total, 1)

    def test_selected_folder_helper_search_uses_the_folder_name(self) -> None:
        service = MailService(registry=mock.Mock())
        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="invoice"),))
        completed: list[tuple] = []

        terms = [{"field": "text", "value": "invoice", "negated": False}]

        def helper_call(method, account, args, kwargs):
            del method, account, args
            if kwargs.get("part") == "header":
                return ([{"uid": "9", "subject": "Invoice"}],)
            if kwargs.get("part") == "body":
                raise RuntimeError("body scan timed out")
            raise AssertionError(kwargs)

        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch.object(
                service, "cancel_account_heavy_folder_index"
            ) as cancel_heavy,
            mock.patch.object(
                service,
                "_camel_helper_call",
                side_effect=helper_call,
            ) as helper_call_mock,
        ):
            service.start_search_folder_messages(
                "acct-1",
                "INBOX",
                query,
                on_complete=completed.append,
            )

        cancel_heavy.assert_called_once_with("acct-1")
        self.assertEqual(
            helper_call_mock.call_args_list,
            [
                mock.call(
                    "search_folder_messages",
                    "acct-1",
                    ["acct-1", "INBOX"],
                    {"terms": terms, "sync": False, "part": "header"},
                ),
                mock.call(
                    "search_folder_messages",
                    "acct-1",
                    ["acct-1", "INBOX"],
                    {"terms": terms, "sync": False, "part": "body"},
                ),
            ],
        )
        self.assertEqual(len(completed), 1)
        messages, _unread, total, source = completed[0]
        self.assertEqual(messages[0]["uid"], "9")
        self.assertEqual(total, 1)
        self.assertEqual(source, "local")


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
        with (
            mock.patch.object(service, "_list_folders_unlocked", return_value=folders),
            mock.patch(
                "post.mail.eds.folder_index_cache.cached_folder_names",
                return_value=[],
            ),
        ):
            ordered = service._ordered_searchable_folders_unlocked("acct-1")
        self.assertEqual([folder["full_name"] for folder in ordered], ["INBOX"])

    def test_ordered_searchable_folders_uses_the_live_tree(self) -> None:
        service = MailService(registry=mock.Mock())
        folders = [
            {"full_name": "INBOX", "display_name": "Inbox", "flags": 0},
        ]
        with (
            mock.patch.object(
                service, "_list_folders_unlocked", return_value=folders
            ),
            mock.patch(
                "post.mail.eds.folder_index_cache.cached_folder_names",
                return_value=["INBOX", "Archive", ".#evolution/Junk"],
            ),
        ):
            ordered = service._ordered_searchable_folders_unlocked("acct-1")
        self.assertEqual(
            [folder["full_name"] for folder in ordered],
            ["INBOX"],
        )

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


class SearchAccountMessagesTests(unittest.TestCase):
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
                "get_account",
                return_value=mock.Mock(display_label="Test Account"),
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
            matched, unread, total, source = service._search_account_messages_unlocked(
                "acct-1",
                query,
            )

        self.assertEqual(unread, 0)
        self.assertEqual(total, 2)
        self.assertEqual(source, "memory")
        self.assertEqual(len(matched), 2)
        self.assertEqual(matched[0]["_search_account_uid"], "acct-1")
        self.assertEqual(matched[0]["_search_folder"], "INBOX")
        self.assertEqual(matched[1]["_search_folder"], "Sent")

    def test_account_search_unions_disk_when_memory_is_smaller(self) -> None:
        ram = [
            {
                "uid": "1",
                "subject": "Hello",
                "from": "other@example.com",
                "sort_date": 1,
                "flags": {"seen": True},
            },
        ]
        disk = ram + [
            {
                "uid": "333",
                "subject": "Order confirmation",
                "from": "Sender <orders@example.net>",
                "sort_date": 2,
                "flags": {"seen": True},
            },
        ]
        service = MailService(registry=mock.Mock())
        service._folder_indexes[("acct-1", "[Google Mail]/All Mail")] = (
            _FolderMessageIndex(messages=ram, unread=0, total=1)
        )
        query = parse_search_query("from: sender")
        assert query is not None

        with (
            mock.patch.object(
                service,
                "get_account",
                return_value=mock.Mock(display_label="Gmail"),
            ),
            mock.patch.object(
                service,
                "_ordered_searchable_folders_unlocked",
                return_value=[
                    {
                        "full_name": "[Google Mail]/All Mail",
                        "display_name": "All Mail",
                    }
                ],
            ),
            mock.patch(
                "post.mail.eds.folder_index_cache.load",
                return_value=(disk, 0, len(disk)),
            ),
            mock.patch.object(service, "_open_folder_unlocked", return_value=mock.Mock()),
            mock.patch("post.mail.eds.folder_get_uids", return_value=["1"]),
            mock.patch("post.mail.eds.folder_get_unread_count", return_value=0),
            mock.patch.object(
                service, "_folder_search_uids_unlocked", return_value=set()
            ),
        ):
            matched, _unread, total, _source = (
                service._search_account_messages_unlocked("acct-1", query)
            )

        self.assertEqual(matched, [])
        self.assertEqual(total, 0)
        self.assertEqual(
            [
                message["uid"]
                for message in service._folder_indexes[
                    ("acct-1", "[Google Mail]/All Mail")
                ].messages
            ],
            ["1"],
        )

    def test_account_search_skips_folder_index_only_folders(
        self,
    ) -> None:
        """A folder that exists only in the retired JSON is not searched (#513)."""
        inbox = [
            {
                "uid": "1",
                "subject": "Inbox",
                "from": "other@example.com",
                "sort_date": 100,
                "flags": {"seen": True},
            },
        ]
        disk_only = [
            {
                "uid": "99",
                "subject": "Old list post",
                "from": "hidden@example.net",
                "sort_date": 50,
                "flags": {"seen": True},
            },
        ]
        service = MailService(registry=mock.Mock())
        service._folder_indexes[("acct-1", "INBOX")] = _FolderMessageIndex(
            messages=inbox, unread=0, total=1
        )
        query = parse_search_query("from: hidden")
        assert query is not None

        def fake_load(account_uid: str, folder_name: str):
            if account_uid == "acct-1" and folder_name == "Lists/Old":
                return (disk_only, 0, 1)
            return None

        with (
            mock.patch.object(
                service,
                "get_account",
                return_value=mock.Mock(display_label="Test"),
            ),
            mock.patch.object(
                service,
                "_list_folders_unlocked",
                return_value=[
                    {"full_name": "INBOX", "display_name": "Inbox", "flags": 0},
                ],
            ),
            mock.patch(
                "post.mail.eds.folder_index_cache.cached_folder_names",
                return_value=["INBOX", "Lists/Old"],
            ),
            mock.patch(
                "post.mail.eds.folder_index_cache.load",
                side_effect=fake_load,
            ),
            mock.patch.object(service, "_open_folder_unlocked", return_value=mock.Mock()),
            mock.patch("post.mail.eds.folder_get_uids", return_value=["1"]),
            mock.patch("post.mail.eds.folder_get_unread_count", return_value=0),
            mock.patch.object(
                service, "_folder_search_uids_unlocked", return_value=set()
            ),
        ):
            matched, _unread, total, _source = (
                service._search_account_messages_unlocked("acct-1", query)
            )

        self.assertEqual(total, 0)
        self.assertEqual(matched, [])

    def test_cancelled_search_returns_empty(self) -> None:
        service = MailService(registry=mock.Mock())
        query = MessageSearchQuery(terms=(SearchTerm(field="subject", value="x"),))
        cancellable = Gio.Cancellable()
        cancellable.cancel()

        with (
            mock.patch.object(
                service, "_begin_folder_search_unlocked", return_value=cancellable
            ),
            mock.patch.object(
                service,
                "_ordered_searchable_folders_unlocked",
                return_value=[],
            ),
        ):
            matched, unread, total, source = service._search_account_messages_unlocked(
                "acct-1",
                query,
            )

        self.assertEqual(matched, [])
        self.assertEqual(unread, 0)
        self.assertEqual(total, 0)
        self.assertEqual(source, "memory")


class FolderIndexHeaderRepairTests(unittest.TestCase):
    def _service_with_index(
        self, messages: list[dict], *, folder_name: str = "INBOX"
    ) -> tuple[MailService, mock.Mock]:
        service = MailService(registry=mock.Mock())
        index = _FolderMessageIndex(
            messages=messages,
            unread=0,
            total=len(messages),
        )
        service._folder_indexes[("acct-1", folder_name)] = index
        folder = mock.Mock()
        return service, folder

    def test_repair_folder_index_message_headers_updates_subject(self) -> None:
        service, _folder = self._service_with_index(
            [
                {
                    "uid": "19025",
                    "subject": "Stale index subject",
                    "from": "a@b.c",
                    "flags": {"seen": True},
                }
            ],
            folder_name="Archive",
        )

        with mock.patch("post.mail.eds.folder_index_cache.save") as save:
            repaired = service.repair_folder_index_message_headers(
                "acct-1",
                "Archive",
                "19025",
                {
                    "uid": "19025",
                    "subject": "Camel subject",
                    "from": "a@b.c",
                },
            )

        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired["subject"], "Camel subject")
        self.assertEqual(repaired["flags"], {"seen": True})
        snapshot = service.get_folder_index_snapshot("acct-1", "Archive")
        assert snapshot is not None
        self.assertEqual(snapshot[0][0]["subject"], "Camel subject")
        save.assert_called_once()

    def test_repair_returns_none_when_headers_agree(self) -> None:
        service, _folder = self._service_with_index(
            [
                {
                    "uid": "1",
                    "subject": "Same",
                    "from": "a@b.c",
                    "flags": {},
                }
            ]
        )

        with mock.patch("post.mail.eds.folder_index_cache.save") as save:
            repaired = service.repair_folder_index_message_headers(
                "acct-1",
                "INBOX",
                "1",
                {"uid": "1", "subject": "same", "from": "a@b.c"},
            )

        self.assertIsNone(repaired)
        save.assert_not_called()

    def test_body_loader_repairs_scan_row_on_cache_hit(self) -> None:
        row = {
            "uid": "19025",
            "subject": "Stale index subject",
            "from": "a@b.c",
            "flags": {"seen": True},
        }
        service, folder = self._service_with_index([row], folder_name="Archive")
        folder.get_message_cached.return_value = object()
        cancellable = Gio.Cancellable()

        with (
            mock.patch(
                "post.mail.helpers.envelope_dict_from_mime",
                return_value={
                    "subject": "Camel subject",
                    "from": "a@b.c",
                },
            ),
            mock.patch(
                "post.mail.helpers.extract_message_bodies",
                return_value={"plain": "query term in body", "html": None},
            ),
            mock.patch("post.mail.eds.folder_index_cache.save"),
        ):
            loader = service._body_text_for_uid_loader(
                folder,
                account_uid="acct-1",
                folder_name="Archive",
                messages_by_uid={"19025": row},
                needs_body=True,
                cancellable=cancellable,
            )
            assert loader is not None
            text = loader("19025")

        self.assertEqual(text, "query term in body")
        self.assertEqual(row["subject"], "Camel subject")
        snapshot = service.get_folder_index_snapshot("acct-1", "Archive")
        assert snapshot is not None
        self.assertEqual(snapshot[0][0]["subject"], "Camel subject")


if __name__ == "__main__":
    unittest.main()
