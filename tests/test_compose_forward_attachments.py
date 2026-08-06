# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Forward compose loads original attachments (#263)."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gtk

from post.compose_window import ComposeWindow
from post.mail.compose import ComposeAttachment
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


class ComposeForwardAttachmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.parent = Gtk.Window()
        self.mail = mock.Mock()
        self.mail.list_sendable_accounts.return_value = [_account()]
        self._idle_callbacks: list = []
        self._io_workers: list = []

        def capture_idle(callback, *args):
            self._idle_callbacks.append((callback, args))
            return 0

        def capture_submit(fn, *args, **kwargs):
            self._io_workers.append((fn, args, kwargs))
            return None

        self._idle_patch = mock.patch(
            "post.compose_window.GLib.idle_add",
            side_effect=capture_idle,
        )
        self._idle_patch.start()
        self._io_patch = mock.patch(
            "post.compose_window.get_mail_io_thread",
        )
        io_mock = self._io_patch.start()
        io_mock.return_value.submit.side_effect = capture_submit
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
        self._io_patch.stop()
        self._sig_patch.stop()
        self._corr_patch.stop()
        self._icon_patch.stop()
        if getattr(self, "window", None) is not None:
            self.window.destroy()
        self.parent.destroy()

    def _run_idle(self, callback, args=()) -> None:
        callback(*args)

    def _drain_forward_load(self) -> None:
        # Run scheduled idles; forward load schedules a worker, then another idle.
        pending = list(self._idle_callbacks)
        self._idle_callbacks.clear()
        for callback, args in pending:
            self._run_idle(callback, args)
        for fn, args, kwargs in list(self._io_workers):
            fn(*args, **kwargs)
        self._io_workers.clear()
        pending = list(self._idle_callbacks)
        self._idle_callbacks.clear()
        for callback, args in pending:
            self._run_idle(callback, args)

    def _open_forward(
        self,
        *,
        attachments_meta: list[dict] | None = None,
        source_folder_name: str | None = "INBOX",
        source_message_uid: str | None = "42",
    ) -> ComposeWindow:
        reply_to = {
            "from": "Author <author@example.com>",
            "to": "user@example.com",
            "subject": "Hello",
            "date": "Thu, 23 Jul 2026 10:00:00 +0000",
            "body_plain": "See attached",
            "attachments": attachments_meta or [],
        }
        self.window = ComposeWindow(
            parent=self.parent,
            mail=self.mail,
            account=_account(),
            set_status=lambda _msg: None,
            mode="forward",
            reply_to=reply_to,
            source_folder_name=source_folder_name,
            source_message_uid=source_message_uid,
        )
        return self.window

    def test_forward_loads_attachments_from_source_message(self) -> None:
        loaded = [
            ComposeAttachment(
                filename="notes.txt",
                mime_type="text/plain",
                data=b"hello",
            )
        ]
        self.mail.read_compose_attachments.return_value = loaded
        window = self._open_forward(
            attachments_meta=[
                {
                    "index": 0,
                    "filename": "notes.txt",
                    "mime_type": "text/plain",
                }
            ]
        )

        self._drain_forward_load()

        self.mail.read_compose_attachments.assert_called_once_with(
            "acct-1",
            "INBOX",
            "42",
        )
        self.assertEqual(len(window._attachments), 1)
        self.assertEqual(window._attachments[0].filename, "notes.txt")
        self.assertEqual(window._attachments[0].data, b"hello")
        self.assertTrue(window._attachments_box.get_visible())

    def test_forward_without_attachments_skips_load(self) -> None:
        window = self._open_forward(attachments_meta=[])

        self._drain_forward_load()

        self.mail.read_compose_attachments.assert_not_called()
        self.assertEqual(window._attachments, [])
        self.assertFalse(window._attachments_box.get_visible())

    def test_forward_without_source_location_skips_load(self) -> None:
        window = self._open_forward(
            attachments_meta=[
                {
                    "index": 0,
                    "filename": "notes.txt",
                    "mime_type": "text/plain",
                }
            ],
            source_folder_name=None,
            source_message_uid=None,
        )

        self._drain_forward_load()

        self.mail.read_compose_attachments.assert_not_called()
        self.assertEqual(window._attachments, [])


if __name__ == "__main__":
    unittest.main()
