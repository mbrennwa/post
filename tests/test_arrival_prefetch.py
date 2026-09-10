# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for live-receive MIME prefetch (#372)."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.offline_sync import (
    ARRIVAL_PREFETCH_BURST,
    OfflineBodySyncCoordinator,
    added_uids_from_change_info,
    select_arrival_prefetch_uids,
)
from post.preferences import (
    OFFLINE_BODY_SYNC_ALL,
    OFFLINE_BODY_SYNC_OFF,
)


class SelectArrivalUidsTests(unittest.TestCase):
    def test_dedupes_and_keeps_order_when_under_limit(self) -> None:
        self.assertEqual(
            select_arrival_prefetch_uids(["a", "b", "a", "c"]),
            ["a", "b", "c"],
        )

    def test_keeps_last_uids_without_dates(self) -> None:
        uids = [str(i) for i in range(25)]
        selected = select_arrival_prefetch_uids(uids)
        self.assertEqual(len(selected), ARRIVAL_PREFETCH_BURST)
        self.assertEqual(selected[0], "5")
        self.assertEqual(selected[-1], "24")

    def test_prefers_newest_dates(self) -> None:
        selected = select_arrival_prefetch_uids(
            ["old", "mid", "new"],
            sort_dates={"old": 1, "mid": 2, "new": 3},
            limit=2,
        )
        self.assertEqual(selected, ["new", "mid"])


class AddedUidsFromChangeInfoTests(unittest.TestCase):
    def test_reads_camel_folder_change_info(self) -> None:
        changes = Camel.FolderChangeInfo.new()
        changes.add_uid("aa")
        changes.add_uid("bb")
        self.assertEqual(added_uids_from_change_info(changes), ["aa", "bb"])

    def test_empty_or_unknown_object(self) -> None:
        self.assertEqual(added_uids_from_change_info(None), [])
        self.assertEqual(added_uids_from_change_info(object()), [])


class ArrivalPrefetchCoordinatorTests(unittest.TestCase):
    def _coordinator(self, *, network: bool = True):
        mail = mock.Mock()
        mail.is_network_available.return_value = network
        mail.offline_body_sync_is_held.return_value = True
        mail._first_cached_rfc822_path.return_value = None
        folder = mock.Mock()
        mail._open_folder_unlocked.return_value = folder
        coordinator = OfflineBodySyncCoordinator(mail)
        return coordinator, mail, folder

    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_OFF,
    )
    def test_off_does_not_queue(self, _mode: mock.Mock, get_io: mock.Mock) -> None:
        coordinator, _mail, _folder = self._coordinator()
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        get_io.return_value.submit_background.assert_not_called()
        self.assertEqual(len(coordinator._arrival_queue), 0)

    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=True,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_user_offline_does_not_queue(
        self, _mode: mock.Mock, _offline: mock.Mock, get_io: mock.Mock
    ) -> None:
        coordinator, _mail, _folder = self._coordinator()
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        get_io.return_value.submit_background.assert_not_called()

    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_no_network_does_not_queue(
        self, _mode: mock.Mock, get_io: mock.Mock
    ) -> None:
        coordinator, _mail, _folder = self._coordinator(network=False)
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        get_io.return_value.submit_background.assert_not_called()

    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_hold_does_not_block_arrival_schedule(
        self, _mode: mock.Mock, _offline: mock.Mock, get_io: mock.Mock
    ) -> None:
        coordinator, mail, _folder = self._coordinator()
        mail.offline_body_sync_is_held.return_value = True
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        get_io.return_value.submit_background.assert_called_once()
        coordinator.schedule_account("acct-1")
        self.assertIn("acct-1", coordinator._running)

    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_burst_caps_at_twenty(
        self, _mode: mock.Mock, _offline: mock.Mock, get_io: mock.Mock
    ) -> None:
        coordinator, _mail, _folder = self._coordinator()
        uids = [str(i) for i in range(25)]
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", uids)
        self.assertEqual(len(coordinator._arrival_queue), ARRIVAL_PREFETCH_BURST)

    @mock.patch("post.mail.offline_sync.folder_get_message_info", return_value=None)
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_skips_nonempty_cache(
        self,
        _mode: mock.Mock,
        _offline: mock.Mock,
        get_io: mock.Mock,
        _info: mock.Mock,
    ) -> None:
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread
        coordinator, mail, folder = self._coordinator()
        mail._first_cached_rfc822_path.return_value = "/tmp/nonempty"
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        worker = io_thread.submit_background.call_args[0][0]
        worker()
        folder.synchronize_message_sync.assert_not_called()

    @mock.patch("post.mail.offline_sync.folder_get_message_info", return_value=None)
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_fetches_zero_byte_or_missing_cache(
        self,
        _mode: mock.Mock,
        _offline: mock.Mock,
        get_io: mock.Mock,
        _info: mock.Mock,
    ) -> None:
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread
        coordinator, mail, folder = self._coordinator()
        mail._first_cached_rfc822_path.return_value = None
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["42"])
        worker = io_thread.submit_background.call_args[0][0]
        worker()
        folder.synchronize_message_sync.assert_called_once()
        args, kwargs = folder.synchronize_message_sync.call_args
        self.assertEqual(args[0], "42")

    @mock.patch("post.mail.offline_sync.folder_get_message_info", return_value=None)
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    @mock.patch(
        "post.mail.offline_sync.account_is_user_offline",
        return_value=False,
    )
    @mock.patch(
        "post.mail.offline_sync.get_account_offline_body_sync",
        return_value=OFFLINE_BODY_SYNC_ALL,
    )
    def test_yields_when_interactive_pending(
        self,
        _mode: mock.Mock,
        _offline: mock.Mock,
        get_io: mock.Mock,
        _info: mock.Mock,
    ) -> None:
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = True
        get_io.return_value = io_thread
        coordinator, _mail, folder = self._coordinator()
        coordinator.schedule_arrival_prefetch("acct-1", "INBOX", ["1"])
        worker = io_thread.submit_background.call_args[0][0]
        worker()
        folder.synchronize_message_sync.assert_not_called()
        self.assertEqual(io_thread.submit_background.call_count, 2)
        self.assertEqual(len(coordinator._arrival_queue), 1)


class MailSyncWatcherArrivalTests(unittest.TestCase):
    def test_folder_changed_schedules_added_uids(self) -> None:
        from post.mail.sync_watcher import MailSyncWatcher

        mail = mock.Mock()
        watcher = MailSyncWatcher(
            mail,
            on_folder_changed=mock.Mock(),
            on_folder_tree_changed=mock.Mock(),
        )
        watcher._running = True
        folder = mock.Mock()
        watcher._folder_to_account[id(folder)] = ("acct-1", "INBOX")
        changes = Camel.FolderChangeInfo.new()
        changes.add_uid("99")
        with mock.patch.object(watcher, "_schedule_folder_changed") as schedule:
            watcher._on_folder_changed_signal(folder, changes)
        mail.schedule_arrival_body_prefetch.assert_called_once_with(
            "acct-1", "INBOX", ["99"]
        )
        schedule.assert_called_once_with("acct-1", "INBOX")
