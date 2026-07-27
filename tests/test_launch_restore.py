# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.mail.eds import MailAccount
from post.sidebar import MailSidebar
from post.window import MainWindow


def _sidebar_state(
    *,
    active_folder: tuple[str, str] | None = None,
    active_message_uid: str | None = None,
) -> dict:
    return {
        "inbox_expanded": True,
        "accounts": {},
        "active_folder": active_folder,
        "active_message_uid": active_message_uid,
        "inbox_order": [],
    }


class EagerRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._mail = mock.Mock()
        self._sidebar = mock.Mock()
        self._load_messages = mock.Mock()
        self._prepare_folder_selection = mock.Mock()

    def _restore(self) -> bool:
        window = mock.Mock()
        window._mail = self._mail
        window._sidebar = self._sidebar
        window._load_messages = self._load_messages
        window._prepare_folder_selection = self._prepare_folder_selection
        return MainWindow._try_eager_restore_active_folder(window)

    def test_returns_false_without_saved_folder(self) -> None:
        with mock.patch(
            "post.window.get_sidebar_state",
            return_value=_sidebar_state(),
        ):
            self.assertFalse(self._restore())
        self._mail.get_account.assert_not_called()
        self._load_messages.assert_not_called()

    def test_returns_false_for_unknown_account(self) -> None:
        self._mail.get_account.side_effect = ValueError("Unknown mail account")
        with mock.patch(
            "post.window.get_sidebar_state",
            return_value=_sidebar_state(active_folder=("missing", "INBOX")),
        ):
            self.assertFalse(self._restore())
        self._load_messages.assert_not_called()

    def test_restores_saved_folder(self) -> None:
        account = MailAccount(
            uid="acct-1",
            name="Test",
            email="user@example.com",
            backend="imapx",
            identity_uid=None,
            from_name=None,
            from_address=None,
            transport_uid=None,
        )
        self._mail.get_account.return_value = account
        sidebar_state = _sidebar_state(
            active_folder=("acct-1", "INBOX"),
            active_message_uid="msg-42",
        )
        with mock.patch("post.window.get_sidebar_state", return_value=sidebar_state):
            self.assertTrue(self._restore())

        self._prepare_folder_selection.assert_called_once_with(
            account, "INBOX", sidebar_state
        )
        self._sidebar.mark_folder_active.assert_called_once_with("acct-1", "INBOX")
        self._load_messages.assert_called_once_with("acct-1", "INBOX")


class MarkFolderActiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self._on_folder_selected = mock.Mock()
        with mock.patch(
            "post.sidebar.get_sidebar_state",
            return_value=_sidebar_state(),
        ):
            self.sidebar = MailSidebar(
                mock.Mock(),
                on_folder_selected=self._on_folder_selected,
                set_status=mock.Mock(),
            )

    def test_mark_folder_active_records_selection(self) -> None:
        self.sidebar.mark_folder_active("acct-1", "INBOX")
        self.assertEqual(self.sidebar._activated_folder, ("acct-1", "INBOX"))
        self._on_folder_selected.assert_not_called()

    def test_activate_folder_row_notifies_when_already_active(self) -> None:
        self.sidebar.mark_folder_active("acct-1", "INBOX")
        self.sidebar._accounts_by_uid["acct-1"] = MailAccount(
            uid="acct-1",
            name="Test",
            email="user@example.com",
            backend="imapx",
            identity_uid=None,
            from_name=None,
            from_address=None,
            transport_uid=None,
        )

        listbox = Gtk.ListBox()
        row = Gtk.ListBoxRow()
        row.account_uid = "acct-1"  # type: ignore[attr-defined]
        row.folder_name = "INBOX"  # type: ignore[attr-defined]
        listbox.append(row)

        self.sidebar._activate_folder_row(listbox, row)

        self._on_folder_selected.assert_called_once()
        account, folder_name = self._on_folder_selected.call_args.args
        self.assertEqual(account.uid, "acct-1")
        self.assertEqual(folder_name, "INBOX")


if __name__ == "__main__":
    unittest.main()
