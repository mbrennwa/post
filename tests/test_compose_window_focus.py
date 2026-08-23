# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for compose focus/scroll (#149, #167) with WebKit body (#206)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

# WebKitGTK network process often needs this in CI/headless environments.
os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("WebKit", "6.0")

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
        self.assertEqual(window._body_view.cursor_offset(), 0)

    def test_reply_initial_focus_keeps_body_at_top(self) -> None:
        window = self._open_reply()
        self.assertGreater(len(window._body_view.get_plain()), 100)

        # Simulate caret moved to EOF, then restore via initial-focus path.
        window._body_view.place_cursor_at_end()
        self._run_captured_idles()
        self._assert_cursor_at_start(window)

    def test_draft_initial_focus_keeps_body_at_top(self) -> None:
        window = self._open_draft()
        self.assertGreater(len(window._body_view.get_plain()), 100)

        window._body_view.place_cursor_at_end()
        self._run_captured_idles()
        self._assert_cursor_at_start(window)

    def test_prefill_places_cursor_at_start(self) -> None:
        window = self._open_reply()
        self._assert_cursor_at_start(window)

    def test_body_editor_is_viewport_sized_not_full_content(self) -> None:
        """Body editor must not inflate to full content height (#167)."""
        window = self._open_reply()
        self._run_captured_idles()
        self._pump()

        editor_height = window._body_view.get_height()
        win_height = window.get_height()
        self.assertGreater(editor_height, 0)
        self.assertGreater(win_height, 0)
        # Slack for chrome; previously the TextView was ~tens of thousands of px.
        self.assertLessEqual(editor_height, win_height + 50)

    def test_header_then_body_focus_does_not_reset_cursor_to_eof(self) -> None:
        """Clicking a header then the body must not jump to EOF (#167)."""
        window = self._open_reply()
        self._run_captured_idles()
        self._pump()

        # Place caret away from start without using the "new mail" focus reset.
        window._body_view.place_cursor_at_end()
        cursor_before = window._body_view.cursor_offset()
        self.assertGreater(cursor_before, 0)

        window._to_entry.grab_focus()
        self._pump()
        window._subject_entry.grab_focus()
        self._pump()
        window._body_view.grab_focus()
        self._pump()

        # Reply mode must not force caret back to start on body focus-in.
        self.assertEqual(window._body_view.cursor_offset(), cursor_before)

    def test_body_is_webkit_editor(self) -> None:
        window = self._open_reply()
        from post.compose_editor import ComposeBodyEditor

        self.assertIsInstance(window._body_view, ComposeBodyEditor)


if __name__ == "__main__":
    unittest.main()
