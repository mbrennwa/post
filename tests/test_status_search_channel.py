# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Search progress owns the status label and bar together (#476)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.window import MainWindow


def _window() -> MainWindow:
    win = MainWindow.__new__(MainWindow)
    win._mail = mock.Mock()
    win._mail.count_queued_drafts.return_value = 0
    win._mail.count_queued_operations.return_value = 0
    win._network_available = True
    win._status = mock.Mock()
    win._status_progress = mock.Mock()
    win._status_progress_pulse_id = None
    win._search_progress_active = False
    win._search_status = ""
    win._search_progress_fraction = None
    win._status_hint = ""
    win._send_delay_status = ""
    win._offline_download_status = ""
    win._message_sync_in_progress = False
    win._offline_held_for_load_generation = None
    win._message_stack = mock.Mock()
    win._message_stack.get_visible_child_name.return_value = "list"
    win._search_query = None
    win._send_delay_scheduler = mock.Mock()
    win._stop_sending_in_flight = False
    win._context_message_uids = []
    return win


class StatusSearchChannelTests(unittest.TestCase):
    def test_hint_during_active_search_keeps_search_chrome(self) -> None:
        win = _window()
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._search_progress_fraction = 0.4
        win._send_delay_status = "Message will be sent in 10 seconds"

        MainWindow._set_status(win, "Sending message…")

        self.assertEqual(win._status_hint, "Sending message…")
        self.assertEqual(win._status.set_label.call_args.args[0], "Searching Archive…")
        self.assertIs(win._status_progress.set_visible.call_args.args[0], True)
        self.assertEqual(win._status_progress.set_fraction.call_args.args[0], 0.4)

    def test_clearing_search_progress_shows_countdown(self) -> None:
        win = _window()
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._search_progress_fraction = 0.4
        win._status_hint = "Sending message…"
        win._send_delay_status = "Message will be sent in 10 seconds"

        MainWindow._hide_status_search_progress(win)

        self.assertFalse(win._search_progress_active)
        self.assertEqual(win._search_status, "")
        self.assertEqual(
            win._status.set_label.call_args.args[0],
            "Message will be sent in 10 seconds",
        )
        self.assertIs(win._status_progress.set_visible.call_args.args[0], False)
        self.assertEqual(win._status_progress.set_fraction.call_args.args[0], 0.0)

    def test_search_results_hint_shows_after_progress_clears(self) -> None:
        win = _window()
        win._search_query = mock.Mock()
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._search_progress_fraction = 0.4
        win._status_hint = "12 matches in Archive"
        win._send_delay_status = "Message will be sent in 10 seconds"

        MainWindow._hide_status_search_progress(win)

        self.assertEqual(
            win._status.set_label.call_args.args[0], "12 matches in Archive"
        )
        self.assertIs(win._status_progress.set_visible.call_args.args[0], False)

    @mock.patch("post.window.list_queued_outbound_messages", return_value=[])
    def test_offline_hides_bar_until_search_can_show_again(
        self, _queued: mock.Mock
    ) -> None:
        win = _window()
        win._network_available = False
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._search_progress_fraction = 0.5

        MainWindow._refresh_status_display(win)

        self.assertTrue(win._search_progress_active)
        self.assertEqual(win._status.set_label.call_args.args[0], "Offline")
        self.assertIs(win._status_progress.set_visible.call_args.args[0], False)

        win._network_available = True
        MainWindow._refresh_status_display(win)

        self.assertEqual(win._status.set_label.call_args.args[0], "Searching Archive…")
        self.assertIs(win._status_progress.set_visible.call_args.args[0], True)
        self.assertEqual(win._status_progress.set_fraction.call_args.args[0], 0.5)

    @mock.patch("post.window.show_toast")
    @mock.patch("post.window.has_pending_send_delay", return_value=True)
    @mock.patch(
        "post.window.list_queued_outbound_messages",
        return_value=[("q1", mock.Mock())],
    )
    def test_send_now_during_search_toasts(
        self,
        _queued: mock.Mock,
        _pending: mock.Mock,
        show_toast: mock.Mock,
    ) -> None:
        win = _window()
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._set_status = mock.Mock()  # type: ignore[method-assign]

        MainWindow._on_status_send_now_clicked(win)

        win._send_delay_scheduler.send_now.assert_called_once_with("q1")
        show_toast.assert_called_once_with(win, "Sending message…")
        win._set_status.assert_not_called()
        self.assertEqual(win._search_status, "Searching Archive…")

    @mock.patch("post.window.show_toast")
    @mock.patch("post.window.has_pending_send_delay", return_value=True)
    @mock.patch(
        "post.window.list_queued_outbound_messages",
        return_value=[("q1", mock.Mock()), ("q2", mock.Mock())],
    )
    def test_send_now_during_search_toasts_plural(
        self,
        _queued: mock.Mock,
        _pending: mock.Mock,
        show_toast: mock.Mock,
    ) -> None:
        win = _window()
        win._search_progress_active = True

        MainWindow._on_status_send_now_clicked(win)

        show_toast.assert_called_once_with(win, "Sending 2 messages…")
        self.assertEqual(win._status_hint, "")

    @mock.patch("post.window.show_toast")
    @mock.patch("post.window.has_pending_send_delay", return_value=True)
    @mock.patch(
        "post.window.list_queued_outbound_messages",
        return_value=[("q1", mock.Mock())],
    )
    def test_send_now_without_search_sets_status(
        self,
        _queued: mock.Mock,
        _pending: mock.Mock,
        show_toast: mock.Mock,
    ) -> None:
        win = _window()

        MainWindow._on_status_send_now_clicked(win)

        show_toast.assert_not_called()
        win._send_delay_scheduler.send_now.assert_called_once_with("q1")
        self.assertEqual(win._status_hint, "Sending message…")
        self.assertEqual(win._status.set_label.call_args.args[0], "Sending message…")
        self.assertIs(win._status_progress.set_visible.call_args.args[0], False)

    @mock.patch("post.window.show_toast")
    def test_outbox_menu_send_now_during_search_toasts(
        self, show_toast: mock.Mock
    ) -> None:
        win = _window()
        win._context_message_uids = ["q1"]
        win._search_progress_active = True
        win._search_status = "Searching Archive…"
        win._set_status = mock.Mock()  # type: ignore[method-assign]

        MainWindow._on_message_menu_outbox_send_now(win)

        win._send_delay_scheduler.send_now.assert_called_once_with("q1")
        show_toast.assert_called_once_with(win, "Sending message…")
        win._set_status.assert_not_called()

    @mock.patch("post.window.show_toast")
    def test_outbox_menu_send_now_without_search_sets_status(
        self, show_toast: mock.Mock
    ) -> None:
        win = _window()
        win._context_message_uids = ["q1"]

        MainWindow._on_message_menu_outbox_send_now(win)

        show_toast.assert_not_called()
        self.assertEqual(win._status_hint, "Sending message…")
        self.assertEqual(win._status.set_label.call_args.args[0], "Sending message…")
