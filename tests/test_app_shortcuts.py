# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""GNOME HIG close/quit accelerators (#325)."""

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, Gtk

from post.app import PostApplication
from post.settings_window import SettingsDialog
from post.window import MainWindow


class AppShortcutAccelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.app = PostApplication()
        self.app._install_standard_shortcuts()

    def tearDown(self) -> None:
        if self.app.lookup_action("quit") is not None:
            self.app.remove_action("quit")

    def test_window_close_accel_is_ctrl_w(self) -> None:
        accels = self.app.get_accels_for_action("window.close")
        self.assertIn("<Control>w", accels)

    def test_app_quit_accel_is_ctrl_q(self) -> None:
        accels = self.app.get_accels_for_action("app.quit")
        self.assertIn("<Control>q", accels)

    def test_quit_action_closes_main_window(self) -> None:
        main = MainWindow.__new__(MainWindow)
        main.close = mock.Mock()
        other = object()
        with mock.patch.object(self.app, "get_windows", return_value=[other, main]):
            with mock.patch.object(self.app, "quit") as quit_fn:
                self.app._on_quit_requested()
        main.close.assert_called_once_with()
        quit_fn.assert_not_called()

    def test_quit_action_falls_back_without_main_window(self) -> None:
        with mock.patch.object(self.app, "get_windows", return_value=[]):
            with mock.patch.object(self.app, "quit") as quit_fn:
                self.app._on_quit_requested()
        quit_fn.assert_called_once_with()


class SettingsWindowShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.app = PostApplication()
        self.app.register()
        self.parent = Adw.ApplicationWindow(application=self.app)

    def tearDown(self) -> None:
        for window in list(self.app.get_windows()):
            window.destroy()

    def test_settings_dialog_registers_with_application(self) -> None:
        mail = mock.Mock()
        mail.registry = mock.Mock()
        mail.list_accounts.return_value = []
        mail.list_sendable_accounts.return_value = []
        with mock.patch(
            "post.settings_window.read_local_mail_config", return_value=None
        ):
            with mock.patch(
                "post.settings_window.is_builtin_local_store_empty",
                return_value=True,
            ):
                dialog = SettingsDialog(
                    parent=self.parent,
                    mail=mail,
                    set_status=lambda _msg: None,
                    on_saved=lambda: None,
                )
        self.assertIs(dialog.get_application(), self.app)
        self.assertIn(dialog, self.app.get_windows())
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
