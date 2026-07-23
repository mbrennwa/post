# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for compose focus/scroll on open (#149)."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import GLib, Gtk

from post.compose_window import ComposeWindow
from post.mail.eds import MailAccount


def _account() -> MailAccount:
    return MailAccount(
        uid="acct-1",
        name="Test",
        email="user@example.com",
        backend="imapx",
        identity_uid=None,
        from_name="User",
        from_address="user@example.com",
        transport_uid="transport-1",
    )


def _long_body(lines: int = 80) -> str:
    return "\n".join(f"quoted line {i}" for i in range(lines))


class ComposeInitialFocusScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.parent = Gtk.Window()
        self.mail = mock.Mock()
        self.mail.list_sendable_accounts.return_value = [_account()]
        self._idle_callbacks: list = []

        def capture_idle(callback, *args):
            self._idle_callbacks.append((callback, args))
            return 0

        self._idle_patch = mock.patch(
            "post.compose_window.GLib.idle_add",
            side_effect=capture_idle,
        )
        self._idle_patch.start()
        self._sig_patch = mock.patch(
            "post.compose_window.get_account_signature",
            return_value="",
        )
        self._sig_patch.start()
        self._corr_patch = mock.patch.object(
            ComposeWindow,
            "_load_correspondents",
            lambda self: None,
        )
        self._corr_patch.start()
        self._icon_patch = mock.patch(
            "post.compose_window.apply_window_icon",
            lambda _window: None,
        )
        self._icon_patch.start()

    def tearDown(self) -> None:
        self._idle_patch.stop()
        self._sig_patch.stop()
        self._corr_patch.stop()
        self._icon_patch.stop()
        if getattr(self, "window", None) is not None:
            self.window.destroy()
        self.parent.destroy()

    def _pump(self) -> None:
        context = GLib.MainContext.default()
        for _ in range(50):
            if not context.iteration(False):
                break

    def _run_captured_idles(self) -> None:
        # Run focus handlers with real idle_add so the follow-up scroll idle works.
        with mock.patch("post.compose_window.GLib.idle_add", GLib.idle_add):
            for callback, args in list(self._idle_callbacks):
                callback(*args)
        self._idle_callbacks.clear()
        self._pump()

    def _open_reply(self) -> ComposeWindow:
        reply_to = {
            "from": "Author <author@example.com>",
            "to": "user@example.com",
            "subject": "Hello",
            "date": "Thu, 23 Jul 2026 10:00:00 +0000",
            "body_plain": _long_body(),
        }
        self.window = ComposeWindow(
            parent=self.parent,
            mail=self.mail,
            account=_account(),
            set_status=lambda _msg: None,
            mode="reply",
            reply_to=reply_to,
        )
        self.window.set_default_size(720, 400)
        self.window.present()
        self._pump()
        return self.window

    def _open_draft(self) -> ComposeWindow:
        draft = {
            "to": "peer@example.com",
            "subject": "Draft",
            "body_plain": _long_body(),
        }
        self.window = ComposeWindow(
            parent=self.parent,
            mail=self.mail,
            account=_account(),
            set_status=lambda _msg: None,
            mode="draft",
            draft_folder_name="Drafts",
            draft_message_uid="1",
            draft_message=draft,
        )
        self.window.set_default_size(720, 400)
        self.window.present()
        self._pump()
        return self.window

    def _assert_cursor_at_start(self, window: ComposeWindow) -> None:
        buffer = window._body_view.get_buffer()
        insert = buffer.get_iter_at_mark(buffer.get_insert())
        self.assertEqual(insert.get_offset(), 0)

    def _assert_form_scrolled_to_top(self, window: ComposeWindow) -> None:
        adj = window._form_scrolled.get_vadjustment()
        self.assertIsNotNone(adj)
        assert adj is not None
        # Allow tiny floating-point / theme rounding.
        self.assertLessEqual(adj.get_value(), adj.get_lower() + 1.0)

    def test_reply_initial_focus_keeps_form_at_top(self) -> None:
        window = self._open_reply()
        buffer = window._body_view.get_buffer()
        self.assertGreater(buffer.get_char_count(), 100)

        # Simulate the old bug: insert at EOF, then scroll to show it.
        buffer.place_cursor(buffer.get_end_iter())
        adj = window._form_scrolled.get_vadjustment()
        self._pump()
        if adj.get_upper() > adj.get_page_size():
            adj.set_value(adj.get_upper() - adj.get_page_size())
            self.assertGreater(adj.get_value(), adj.get_lower())

        self._run_captured_idles()
        self._assert_cursor_at_start(window)
        self._assert_form_scrolled_to_top(window)

    def test_draft_initial_focus_keeps_form_at_top(self) -> None:
        window = self._open_draft()
        buffer = window._body_view.get_buffer()
        self.assertGreater(buffer.get_char_count(), 100)

        buffer.place_cursor(buffer.get_end_iter())
        adj = window._form_scrolled.get_vadjustment()
        self._pump()
        if adj.get_upper() > adj.get_page_size():
            adj.set_value(adj.get_upper() - adj.get_page_size())

        self._run_captured_idles()
        self._assert_cursor_at_start(window)
        self._assert_form_scrolled_to_top(window)

    def test_prefill_places_cursor_at_start(self) -> None:
        window = self._open_reply()
        self._assert_cursor_at_start(window)


if __name__ == "__main__":
    unittest.main()
