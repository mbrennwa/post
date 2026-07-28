# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for heavy-folder index grow-only cache helpers (#208)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import _should_save_heavy_folder_index


class HeavyFolderIndexCacheTests(unittest.TestCase):
    def test_save_when_no_existing(self) -> None:
        self.assertTrue(
            _should_save_heavy_folder_index([{"uid": "1"}], None)
        )
        self.assertFalse(_should_save_heavy_folder_index([], None))

    def test_save_only_when_growing(self) -> None:
        existing = ([{"uid": "1"}, {"uid": "2"}], 0, 100)
        self.assertFalse(
            _should_save_heavy_folder_index([{"uid": "1"}], existing)
        )
        self.assertFalse(
            _should_save_heavy_folder_index(
                [{"uid": "1"}, {"uid": "2"}], existing
            )
        )
        self.assertTrue(
            _should_save_heavy_folder_index(
                [{"uid": "1"}, {"uid": "2"}, {"uid": "3"}], existing
            )
        )


class OfflineSyncNoHeavyIndexTests(unittest.TestCase):
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    def test_run_account_sync_does_not_call_heavy_folder_index(
        self, get_io: mock.Mock,
    ) -> None:
        """Offline body sync may re-index local summary only (no refresh_info)."""
        from post.mail.offline_sync import OfflineBodySyncCoordinator

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread

        mail = mock.Mock()
        mail.get_account.return_value = mock.Mock(display_label="Test")
        mail.continue_heavy_folder_index = mock.Mock()
        coordinator = OfflineBodySyncCoordinator(mail)

        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        offline_folder = mock.Mock(spec=Camel.OfflineFolder)
        offline_folder.get_full_name.return_value = "Archive"
        offline_folder.can_downsync.return_value = True
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = False

        with mock.patch(
            "post.mail.offline_sync.apply_offline_sync_to_folder"
        ):
            with mock.patch.object(coordinator, "_downsync_folder_sync") as downsync:
                complete = coordinator._run_account_sync(
                    "acct-1",
                    "all",
                    cancellable,
                    folders=[offline_folder],
                    folder_index=0,
                )

        self.assertTrue(complete)
        downsync.assert_called_once()
        mail.continue_heavy_folder_index.assert_called_once_with(
            "acct-1", "Archive", allow_refresh=False
        )


if __name__ == "__main__":
    unittest.main()
