# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""UI must not 2s-retry heavy-folder catch-up after incomplete-delta abandon (#441)."""

from __future__ import annotations

import unittest
from unittest import mock


class HeavyFolderUiAbandonRetryTests(unittest.TestCase):
    @mock.patch("post.window.GLib")
    def test_abandoned_done_does_not_schedule_catchup_retry(
        self, glib: mock.Mock
    ) -> None:
        from post.window import MainWindow

        win = MainWindow.__new__(MainWindow)
        win._messages_load_generation = 1
        win._search_query = None
        win._current_account = mock.Mock(uid="acct")
        win._current_folder = "Archive"
        win._mail = mock.Mock()
        win._mail.get_folder_status_totals.return_value = (2474, 18885)
        win._set_status = mock.Mock()
        win._message_sync_in_progress = True
        win._heavy_index_in_progress = ("acct", "Archive")
        win._current_folder_messages = [{"uid": "1"}]
        win._message_total = 1
        win._message_list_view = mock.Mock()
        win._message_list_view.item_count.return_value = 0
        win._message_list_bound_count = 0
        win._heavy_pipeline_id = None
        win._start_background_heavy_folder_index = mock.Mock()
        win._remap_folder_message_uids = mock.Mock()
        win._update_message_status = mock.Mock()
        win._schedule_bind_unbound_heavy_messages = mock.Mock()
        win._apply_folder_messages = mock.Mock()

        progress = mock.Mock()
        progress.done = True
        progress.messages = []
        progress.cursor = {"incomplete_delta_abandoned": True}
        progress.uid_remaps = {}

        with mock.patch(
            "post.window.message_lists_equivalent_for_ui", return_value=True
        ), mock.patch(
            "post.window.message_list_fingerprint", return_value="fp"
        ):
            win._on_heavy_folder_index_progress(
                1, "acct", "Archive", progress
            )

        glib.timeout_add.assert_not_called()
        win._start_background_heavy_folder_index.assert_not_called()
        self.assertFalse(win._message_sync_in_progress)
        self.assertIsNone(win._heavy_index_in_progress)

    @mock.patch("post.window.GLib")
    def test_behind_status_still_retries_when_not_abandoned(
        self, glib: mock.Mock
    ) -> None:
        from post.window import MainWindow

        win = MainWindow.__new__(MainWindow)
        win._messages_load_generation = 1
        win._search_query = None
        win._current_account = mock.Mock(uid="acct")
        win._current_folder = "Archive"
        win._mail = mock.Mock()
        win._mail.get_folder_status_totals.return_value = (2474, 18885)
        win._set_status = mock.Mock()
        win._message_sync_in_progress = False
        win._heavy_index_in_progress = ("acct", "Archive")
        win._current_folder_messages = []
        win._message_total = 0
        win._message_list_view = mock.Mock()
        win._message_list_view.item_count.return_value = 0
        win._message_list_bound_count = 0
        win._heavy_pipeline_id = None
        win._start_background_heavy_folder_index = mock.Mock()
        win._remap_folder_message_uids = mock.Mock()
        win._update_message_status = mock.Mock()
        win._schedule_bind_unbound_heavy_messages = mock.Mock()
        win._apply_folder_messages = mock.Mock()

        progress = mock.Mock()
        progress.done = True
        # Indexed far behind trusted STATUS, not abandoned → catch-up retry.
        progress.messages = [{"uid": str(i)} for i in range(500)]
        progress.cursor = {"incomplete_delta_abandoned": False}
        progress.uid_remaps = {}

        with mock.patch(
            "post.window.message_lists_equivalent_for_ui", return_value=True
        ), mock.patch(
            "post.window.message_list_fingerprint", return_value="fp"
        ):
            win._on_heavy_folder_index_progress(
                1, "acct", "Archive", progress
            )

        glib.timeout_add.assert_called_once()
        self.assertEqual(glib.timeout_add.call_args.args[0], 2000)


if __name__ == "__main__":
    unittest.main()
