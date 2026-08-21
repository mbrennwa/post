# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for compose file-drop attachment helpers (#135 Phase 1)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gdk, Gio, Gtk

from post.compose_window import ComposeWindow, _paths_from_drop_value
from post.mail.compose import load_attachment_from_path
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


class LoadAttachmentFromPathTests(unittest.TestCase):
    def test_loads_file_bytes_and_mime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.txt")
            with open(path, "wb") as handle:
                handle.write(b"hello attachments")
            attachment = load_attachment_from_path(path)
        self.assertEqual(attachment.filename, "note.txt")
        self.assertEqual(attachment.data, b"hello attachments")
        self.assertTrue(attachment.mime_type)

    def test_rejects_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IsADirectoryError):
                load_attachment_from_path(tmp)


class PathsFromDropValueTests(unittest.TestCase):
    def test_file_list_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            with open(path, "wb") as handle:
                handle.write(b"x")
            file_list = Gdk.FileList.new_from_list([Gio.File.new_for_path(path)])
        self.assertEqual(_paths_from_drop_value(file_list), [path])

    def test_single_gio_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.bin")
            with open(path, "wb") as handle:
                handle.write(b"y")
            gfile = Gio.File.new_for_path(path)
        self.assertEqual(_paths_from_drop_value(gfile), [path])

    def test_ignores_unknown_value(self) -> None:
        self.assertEqual(_paths_from_drop_value("not-a-file"), [])


class ComposeFileDropTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.parent = Gtk.Window()
        self.mail = mock.Mock()
        self.mail.list_sendable_accounts.return_value = [_account()]
        self._idle_patch = mock.patch(
            "post.compose_window.GLib.idle_add",
            return_value=0,
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
        self.parent.destroy()

    def _open_compose(self) -> ComposeWindow:
        return ComposeWindow(
            parent=self.parent,
            mail=self.mail,
            account=_account(),
            set_status=lambda _msg: None,
            mode="new",
        )

    def test_add_attachments_from_paths(self) -> None:
        window = self._open_compose()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "photo.png")
            with open(path, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n")
            added = window._add_attachments_from_paths([path])
        self.assertEqual(added, 1)
        self.assertEqual(len(window._attachments), 1)
        self.assertEqual(window._attachments[0].filename, "photo.png")
        self.assertTrue(window._attachments_box.get_visible())
        window.destroy()

    def test_add_attachments_skips_directory_with_toast(self) -> None:
        window = self._open_compose()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("post.compose_window.show_error_toast") as toast,
        ):
            added = window._add_attachments_from_paths([tmp])
        self.assertEqual(added, 0)
        self.assertEqual(window._attachments, [])
        toast.assert_called_once()
        window.destroy()

    def test_drop_targets_installed(self) -> None:
        window = self._open_compose()
        overlay_controllers = list(window._toast_overlay.observe_controllers())
        body_controllers = list(window._body_view.observe_controllers())
        self.assertTrue(
            any(isinstance(c, Gtk.DropTarget) for c in overlay_controllers)
        )
        self.assertTrue(any(isinstance(c, Gtk.DropTarget) for c in body_controllers))
        window.destroy()


if __name__ == "__main__":
    unittest.main()
