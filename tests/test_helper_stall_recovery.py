# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""A stalled helper must not blank the inbox or outlive a finished move (#549)."""

from __future__ import annotations

import time
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

from post.mail.camel_runtime import CamelHelperBusy
from post.mail.eds import (
    CANCEL_SCOPE_BACKGROUND,
    HELPER_OP_BACKGROUND,
    MailService,
    _LOCAL_LIST_WAIT_SECONDS,
)


def _rows(count: int) -> list[dict]:
    return [
        {"uid": str(index), "subject": f"m{index}", "sort_date": 1000 - index}
        for index in range(count)
    ]


class LocalListWhileBusyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        patcher = mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = mock.Mock()
        self.service._camel_pool = self.pool
        preempt = mock.patch.object(self.service, "preempt_background_op")
        self.preempt = preempt.start()
        self.addCleanup(preempt.stop)

    def _list(self, *, sync: bool = False, **kwargs):
        return self.service.get_folder_messages(
            "acct", "INBOX", sync=sync, **kwargs
        )

    def test_local_list_gives_up_waiting_quickly(self) -> None:
        with mock.patch.object(
            self.service,
            "_camel_helper_call",
            return_value=(_rows(2), 1, 2, "local"),
        ) as call:
            self._list()
        self.assertEqual(call.call_args.kwargs["wait_timeout"], _LOCAL_LIST_WAIT_SECONDS)

    def test_server_list_waits_for_the_helper(self) -> None:
        with mock.patch.object(
            self.service,
            "_camel_helper_call",
            return_value=(_rows(2), 1, 2, "server"),
        ) as call:
            self._list(sync=True)
        self.assertIsNone(call.call_args.kwargs["wait_timeout"])

    def test_busy_helper_is_answered_from_its_memory(self) -> None:
        self.pool.peek_summary_page.return_value = (_rows(3), 1, 3, "memory")
        with mock.patch.object(
            self.service, "_camel_helper_call", side_effect=CamelHelperBusy("busy")
        ) as call:
            messages, unread, total, source = self._list()
        self.assertEqual(len(messages), 3)
        self.assertEqual((unread, total, source), (1, 3, "memory"))
        call.assert_called_once()
        self.preempt.assert_called_once_with("acct")

    def test_busy_helper_without_memory_uses_the_disk_index(self) -> None:
        self.pool.peek_summary_page.return_value = ([], -1, -1, "memory")
        with (
            mock.patch.object(
                self.service,
                "_camel_helper_call",
                side_effect=CamelHelperBusy("busy"),
            ),
            mock.patch(
                "post.mail.eds.folder_index_cache.load",
                return_value=(_rows(5), 2, 5),
            ),
        ):
            messages, unread, total, source = self._list(offset=1, limit=2)
        self.assertEqual([m["uid"] for m in messages], ["1", "2"])
        self.assertEqual((unread, total, source), (2, 5, "disk_cache"))

    def test_pending_local_removals_still_apply(self) -> None:
        self.pool.peek_summary_page.return_value = (_rows(3), 0, 3, "memory")
        with (
            mock.patch.object(
                self.service,
                "_camel_helper_call",
                side_effect=CamelHelperBusy("busy"),
            ),
            mock.patch.object(
                self.service, "_pending_uids_for_folder", return_value={"1"}
            ),
        ):
            messages, *_rest = self._list()
        self.assertEqual([m["uid"] for m in messages], ["0", "2"])

    def test_nothing_local_waits_for_the_helper(self) -> None:
        self.pool.peek_summary_page.return_value = ([], -1, -1, "memory")
        with (
            mock.patch.object(
                self.service,
                "_camel_helper_call",
                side_effect=[CamelHelperBusy("busy"), (_rows(1), 0, 1, "local")],
            ) as call,
            mock.patch("post.mail.eds.folder_index_cache.load", return_value=None),
        ):
            messages, _unread, _total, source = self._list()
        self.assertEqual(len(messages), 1)
        self.assertEqual(source, "local")
        self.assertEqual(call.call_count, 2)
        self.assertNotIn("wait_timeout", call.call_args_list[1].kwargs)


class BestEffortScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())

    def test_scope_is_preemptible_background_work(self) -> None:
        with self.service._best_effort_scope(60) as cancellable:
            self.assertFalse(cancellable.is_cancelled())
            self.service.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
            self.assertTrue(cancellable.is_cancelled())

    def test_scope_unregisters_on_exit(self) -> None:
        with self.service._best_effort_scope(60) as cancellable:
            pass
        self.assertIsNone(self.service._helper_op_cancellable)
        self.service.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
        self.assertFalse(cancellable.is_cancelled())

    def test_scope_expires_on_its_own(self) -> None:
        with self.service._best_effort_scope(0.05) as cancellable:
            for _ in range(200):
                if cancellable.is_cancelled():
                    break
                time.sleep(0.01)
            self.assertTrue(cancellable.is_cancelled())

    def test_scope_kind_is_background(self) -> None:
        with self.service._best_effort_scope(60):
            self.assertEqual(self.service._helper_op_kind, HELPER_OP_BACKGROUND)


class PostMoveTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        self.folder = mock.Mock()

    def test_prefetch_passes_the_cancellable_to_camel(self) -> None:
        cancellable = Gio.Cancellable()
        self.service._sync_moved_destination_uids_unlocked(
            self.folder, ["1"], cancellable
        )
        args = self.folder.synchronize_message_sync.call_args.args
        self.assertIs(args[1], cancellable)

    def test_prefetch_stops_once_cancelled(self) -> None:
        cancellable = Gio.Cancellable()

        def fetch(_uid, _cancellable):
            cancellable.cancel()

        self.folder.synchronize_message_sync.side_effect = fetch
        result = self.service._sync_moved_destination_uids_unlocked(
            self.folder, ["1", "2", "3"], cancellable
        )
        self.assertEqual(self.folder.synchronize_message_sync.call_count, 1)
        # The UIDs are known, so they are still reported for Undo.
        self.assertEqual(result, ["1", "2", "3"])

    def test_cancelled_scan_gives_up(self) -> None:
        cancellable = Gio.Cancellable()
        cancellable.cancel()
        with (
            mock.patch("post.mail.eds.folder_get_uids", return_value=["1", "2"]),
            mock.patch("post.mail.eds.folder_get_message_info") as info,
        ):
            found = self.service._find_moved_uids_in_folder_unlocked(
                self.folder,
                [{"subject": "s", "from": "f", "sort_date": 1}],
                cancellable=cancellable,
            )
        self.assertEqual(found, [])
        info.assert_not_called()

    def test_cancelled_scope_skips_destination_refresh(self) -> None:
        parent = Gio.Cancellable()
        parent.cancel()
        self.service._refresh_destination_folder_bounded_unlocked(
            self.folder, timeout=5, parent=parent
        )
        self.folder.refresh_info_sync.assert_not_called()

    def test_parent_cancel_reaches_a_running_refresh(self) -> None:
        parent = Gio.Cancellable()
        seen: list[bool] = []

        def refresh(cancellable):
            parent.cancel()
            seen.append(cancellable.is_cancelled())

        self.folder.refresh_info_sync.side_effect = refresh
        self.service._refresh_destination_folder_bounded_unlocked(
            self.folder, timeout=5, parent=parent
        )
        self.assertEqual(seen, [True])


class HeavyDestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        self.folder = mock.Mock()

    def _resolve(self, folder_name: str, backend: str):
        self.folder.get_full_name.return_value = folder_name
        with mock.patch.object(
            self.service,
            "_find_moved_uids_in_folder_unlocked",
            return_value=[],
        ):
            return self.service._resolve_and_sync_moved_destination_uids_unlocked(
                self.folder,
                [{"subject": "s"}],
                [],
                moved_count=1,
                backend=backend,
            )

    def test_imap_archive_is_not_refreshed(self) -> None:
        self.assertEqual(self._resolve("Archive", "imapx"), [])
        self.folder.refresh_info_sync.assert_not_called()

    def test_graph_archive_is_not_refreshed_either(self) -> None:
        self.assertEqual(self._resolve("Archive", "microsoft365"), [])
        self.folder.refresh_info_sync.assert_not_called()

    def test_small_imap_folder_is_still_refreshed(self) -> None:
        self._resolve("Projects", "imapx")
        self.folder.refresh_info_sync.assert_called_once()


class CommitIsNotPreemptibleTests(unittest.TestCase):
    def test_commit_scope_ignores_foreground_preemption(self) -> None:
        service = MailService(registry=mock.Mock())
        with service._best_effort_scope(60, preemptible=False) as cancellable:
            service.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
            self.assertFalse(cancellable.is_cancelled())


class TransferBackendTests(unittest.TestCase):
    def test_helper_resolves_backend_without_listing_accounts(self) -> None:
        service = MailService(registry=mock.Mock())
        self.assertEqual(service._accounts_by_uid, {})
        with mock.patch.object(
            service, "_backend_for_account", return_value="Microsoft365"
        ):
            self.assertEqual(service._transfer_backend_unlocked("acct"), "microsoft365")

    def test_unknown_backend_is_empty(self) -> None:
        service = MailService(registry=mock.Mock())
        with mock.patch.object(service, "_backend_for_account", return_value=None):
            self.assertEqual(service._transfer_backend_unlocked("acct"), "")

    def test_graph_account_skips_post_transfer_sync_in_a_helper(self) -> None:
        service = MailService(registry=mock.Mock())
        source, dest = mock.Mock(), mock.Mock()
        with mock.patch.object(
            service, "_backend_for_account", return_value="microsoft365"
        ):
            service._finalize_folder_transfer_unlocked(
                "acct",
                source,
                dest,
                source_folder_name="Inbox",
                dest_name="Archive",
            )
        source.synchronize_sync.assert_not_called()
        source.refresh_info_sync.assert_not_called()
        dest.refresh_info_sync.assert_not_called()


class OrphanedPendingRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        store = mock.patch("post.mail.eds.pending_removals_store")
        self.store = store.start()
        self.addCleanup(store.stop)
        self.store.load_all_pending_removals.return_value = {
            ("acct", "Inbox"): {"1", "2"}
        }
        self.store.pending_age_seconds.return_value = 600.0
        clear = mock.patch.object(self.service, "_clear_pending_local_removals")
        self.clear = clear.start()
        self.addCleanup(clear.stop)

    def _queued(self, *uid_lists: list[str]):
        return [
            (
                f"q{index}",
                mock.Mock(account_uid="acct", folder_name="Inbox", message_uids=uids),
            )
            for index, uids in enumerate(uid_lists)
        ]

    def _prune(self, queued) -> None:
        with mock.patch("post.mail.eds.list_queued_operations", return_value=queued):
            self.service._prune_orphaned_pending_removals("acct")

    def test_marker_without_a_queued_operation_is_dropped(self) -> None:
        self._prune([])
        self.clear.assert_called_once_with("acct", "Inbox", ["1", "2"])

    def test_uids_covered_by_a_queued_operation_stay_hidden(self) -> None:
        self._prune(self._queued(["1"]))
        self.clear.assert_called_once_with("acct", "Inbox", ["2"])

    def test_fully_covered_marker_is_kept(self) -> None:
        self._prune(self._queued(["1", "2"]))
        self.clear.assert_not_called()

    def test_young_marker_is_kept(self) -> None:
        self.store.pending_age_seconds.return_value = 5.0
        self._prune([])
        self.clear.assert_not_called()

    def test_other_accounts_are_untouched(self) -> None:
        self.store.load_all_pending_removals.return_value = {
            ("other", "Inbox"): {"9"}
        }
        self._prune([])
        self.clear.assert_not_called()

    def test_every_account_is_covered_without_an_argument(self) -> None:
        self.store.load_all_pending_removals.return_value = {
            ("a", "Inbox"): {"1"},
            ("b", "Inbox"): {"2"},
        }
        with mock.patch("post.mail.eds.list_queued_operations", return_value=[]):
            self.service._prune_orphaned_pending_removals()
        self.assertEqual(self.clear.call_count, 2)

    def test_flush_prunes_even_when_the_queue_is_empty(self) -> None:
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch("post.mail.eds.list_queued_operations", return_value=[]),
            mock.patch.object(
                self.service, "_prune_orphaned_pending_removals"
            ) as prune,
            mock.patch.object(self.service, "_reload_pending_local_removals_from_disk"),
            mock.patch.object(self.service, "_notify_operation_flush_done"),
        ):
            self.service.flush_operation_queue()
        prune.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
