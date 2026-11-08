# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Forward compose loads original attachments (#263, #512)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gtk

from post.compose_window import (
    ComposeWindow,
    _FORWARD_ATTACHMENTS_INCOMPLETE,
    _FORWARD_ATTACHMENTS_LOADING,
    _FORWARD_ATTACHMENTS_MISSING_SOURCE,
)
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

        def capture_submit(_name, fn, *args, **kwargs):
            self._io_workers.append((fn, args, kwargs))
            return None

        self._idle_patch = mock.patch(
            "post.compose_window.GLib.idle_add",
            side_effect=capture_idle,
        )
        self._idle_patch.start()
        self.mail.submit_interactive.side_effect = capture_submit
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

    def _fill_recipient(self, window: ComposeWindow) -> None:
        window._tracking_edits = False
        window._to_entry.set_text("bob@example.com")
        window._tracking_edits = True
        window._update_field_hints()
        window._update_send_enabled()
        window._update_save_draft_enabled()

    def _mark_edited(self, window: ComposeWindow) -> None:
        window._user_edited = True
        window._update_save_draft_enabled()

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
        self._fill_recipient(window)
        self.assertTrue(window._send_btn.get_sensitive())

    def test_forward_without_source_location_blocks_send(self) -> None:
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
        self.assertEqual(
            window._forward_attachments_error,
            _FORWARD_ATTACHMENTS_MISSING_SOURCE,
        )
        self.assertIn(
            _FORWARD_ATTACHMENTS_MISSING_SOURCE,
            _widget_labels(window._attachments_box),
        )
        self.assertIsNone(_button_with_label(window._attachments_box, "Retry"))
        self._fill_recipient(window)
        self._mark_edited(window)
        self.assertFalse(window._send_btn.get_sensitive())
        self.assertFalse(window._save_draft_btn.get_sensitive())

    def test_forward_blocks_send_until_attachments_load(self) -> None:
        loaded = [_attachment("notes.txt")]
        self.mail.read_compose_attachments.return_value = loaded
        window = self._open_forward(attachments_meta=_attachment_meta("notes.txt"))
        self._fill_recipient(window)
        self._mark_edited(window)

        self.assertTrue(window._forward_attachments_pending)
        self.assertIn(
            _FORWARD_ATTACHMENTS_LOADING,
            _widget_labels(window._attachments_box),
        )
        self.assertTrue(window._attach_files_btn.get_sensitive())
        self.assertFalse(window._send_btn.get_sensitive())
        self.assertFalse(window._save_draft_btn.get_sensitive())
        with mock.patch.object(window, "_proceed_with_send") as proceed:
            window._on_send_clicked()
            proceed.assert_not_called()
        window._begin_save_draft(close_when_done=False)
        self.assertFalse(window._saving_draft)

        self._drain_forward_load()

        self.assertFalse(window._forward_attachments_pending)
        self.assertIsNone(window._forward_attachments_error)
        self.assertNotIn(
            _FORWARD_ATTACHMENTS_LOADING,
            _widget_labels(window._attachments_box),
        )
        self.assertEqual(window._attachments[0].filename, "notes.txt")
        self.assertTrue(window._send_btn.get_sensitive())
        self.assertTrue(window._save_draft_btn.get_sensitive())

    def test_forward_load_error_keeps_send_disabled_until_retry(self) -> None:
        self.mail.read_compose_attachments.side_effect = OSError("offline")
        window = self._open_forward(attachments_meta=_attachment_meta("notes.txt"))
        self._fill_recipient(window)
        self._mark_edited(window)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            with open(path, "wb") as handle:
                handle.write(b"png")
            self.assertEqual(window._add_attachments_from_paths([path]), 1)
            self._drain_forward_load()

        self.assertEqual(
            [item.filename for item in window._attachments],
            ["photo.png"],
        )
        self.assertEqual(
            window._forward_attachments_error,
            "Attachment error: offline",
        )
        self.assertFalse(window._send_btn.get_sensitive())
        self.assertFalse(window._save_draft_btn.get_sensitive())
        retry = _button_with_label(window._attachments_box, "Retry")
        self.assertIsNotNone(retry)

        loaded = [_attachment("notes.txt")]
        self.mail.read_compose_attachments.side_effect = None
        self.mail.read_compose_attachments.return_value = loaded
        assert retry is not None
        retry.emit("clicked")
        self._drain_forward_load()

        self.assertEqual(self.mail.read_compose_attachments.call_count, 2)
        self.assertEqual(
            [item.filename for item in window._attachments],
            ["notes.txt", "photo.png"],
        )
        self.assertIsNone(window._forward_attachments_error)
        self.assertTrue(window._send_btn.get_sensitive())
        self.assertTrue(window._save_draft_btn.get_sensitive())

    def test_forward_empty_load_is_failure(self) -> None:
        self._assert_incomplete_load(returned=[])

    def test_forward_short_load_is_failure(self) -> None:
        self._assert_incomplete_load(returned=[_attachment("notes.txt")])

    def _assert_incomplete_load(self, *, returned: list[ComposeAttachment]) -> None:
        self.mail.read_compose_attachments.return_value = returned
        window = self._open_forward(
            attachments_meta=_attachment_meta("notes.txt", "ticket.pdf")
        )
        self._fill_recipient(window)
        self._mark_edited(window)
        self._drain_forward_load()

        self.assertEqual(window._attachments, [])
        self.assertEqual(
            window._forward_attachments_error,
            _FORWARD_ATTACHMENTS_INCOMPLETE,
        )
        self.assertFalse(window._send_btn.get_sensitive())
        self.assertFalse(window._save_draft_btn.get_sensitive())
        retry = _button_with_label(window._attachments_box, "Retry")
        self.assertIsNotNone(retry)

        self.mail.read_compose_attachments.return_value = [
            _attachment("notes.txt"),
            _attachment("ticket.pdf", b"pdf"),
        ]
        assert retry is not None
        retry.emit("clicked")
        self._drain_forward_load()

        self.assertEqual(self.mail.read_compose_attachments.call_count, 2)
        self.assertEqual(
            [item.filename for item in window._attachments],
            ["notes.txt", "ticket.pdf"],
        )
        self.assertTrue(window._send_btn.get_sensitive())

    def test_forward_keeps_user_files_added_during_load(self) -> None:
        self.mail.read_compose_attachments.return_value = [_attachment("notes.txt")]
        window = self._open_forward(attachments_meta=_attachment_meta("notes.txt"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            with open(path, "wb") as handle:
                handle.write(b"png")
            self.assertEqual(window._add_attachments_from_paths([path]), 1)
            self._drain_forward_load()

        self.assertEqual(
            [item.filename for item in window._attachments],
            ["notes.txt", "photo.png"],
        )


def _attachment(filename: str, data: bytes = b"hello") -> ComposeAttachment:
    return ComposeAttachment(
        filename=filename,
        mime_type="text/plain",
        data=data,
    )


def _attachment_meta(*filenames: str) -> list[dict]:
    return [
        {"index": index, "filename": filename, "mime_type": "text/plain"}
        for index, filename in enumerate(filenames)
    ]


def _widget_labels(widget: Gtk.Widget) -> list[str]:
    found: list[str] = []

    def walk(child: Gtk.Widget | None) -> None:
        while child is not None:
            if isinstance(child, Gtk.Label):
                found.append(child.get_label())
            walk(child.get_first_child())
            child = child.get_next_sibling()

    walk(widget.get_first_child())
    return found


def _button_with_label(widget: Gtk.Widget, label: str) -> Gtk.Button | None:
    found: Gtk.Button | None = None

    def walk(child: Gtk.Widget | None) -> None:
        nonlocal found
        while child is not None:
            if isinstance(child, Gtk.Button) and child.get_label() == label:
                found = child
            walk(child.get_first_child())
            child = child.get_next_sibling()

    walk(widget.get_first_child())
    return found


if __name__ == "__main__":
    unittest.main()
