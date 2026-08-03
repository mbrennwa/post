# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.mail.eds import MailAccount
from post.preferences import resolve_inbox_display_order
from post.sidebar import MailSidebar


def _sidebar_state(*, inbox_order: list[str] | None = None) -> dict:
    return {
        "inbox_expanded": True,
        "accounts": {},
        "active_folder": None,
        "active_message_uid": None,
        "inbox_order": list(inbox_order or []),
    }


def _account(uid: str, name: str | None = None) -> MailAccount:
    return MailAccount(
        uid=uid,
        name=name or uid,
        email=f"{uid}@example.com",
        backend="imapx",
        identity_uid=None,
        from_name=None,
        from_address=None,
        transport_uid=None,
    )


def _account_section_uids(sidebar: MailSidebar) -> list[str]:
    uids: list[str] = []
    child = sidebar._sidebar_box.get_first_child()
    while child is not None:
        if child is sidebar._inbox_expander:
            child = child.get_next_sibling()
            continue
        uid = getattr(child, "account_uid", None)
        if isinstance(uid, str) and uid:
            uids.append(uid)
        child = child.get_next_sibling()
    return uids


class ResolveInboxOrderTests(unittest.TestCase):
    def test_load_style_ordering_prefers_saved_order(self) -> None:
        saved = ["acct-b", "acct-a", "acct-c"]
        # list_accounts()-style present order differs from saved.
        present = ["acct-a", "acct-b", "acct-c"]
        self.assertEqual(
            resolve_inbox_display_order(saved, present),
            ["acct-b", "acct-a", "acct-c"],
        )


class InboxAccountSectionOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def setUp(self) -> None:
        self.accounts = [
            _account("acct-a", "A"),
            _account("acct-b", "B"),
            _account("acct-c", "C"),
        ]
        self.mail = mock.Mock()
        self.mail.list_accounts.return_value = list(self.accounts)
        self.mail.get_inbox_folder_name_cached.return_value = "INBOX"

        with mock.patch(
            "post.sidebar.get_sidebar_state",
            return_value=_sidebar_state(inbox_order=["acct-c", "acct-a", "acct-b"]),
        ):
            self.sidebar = MailSidebar(
                self.mail,
                on_folder_selected=mock.Mock(),
                set_status=mock.Mock(),
            )

    def _load_sidebar(self) -> None:
        with (
            mock.patch.object(self.sidebar, "_start_folder_load"),
            mock.patch("post.sidebar.set_sidebar_state"),
        ):
            self.sidebar.load()

    def test_load_appends_account_sections_in_saved_inbox_order(self) -> None:
        self._load_sidebar()
        self.assertEqual(
            _account_section_uids(self.sidebar),
            ["acct-c", "acct-a", "acct-b"],
        )
        self.assertEqual(
            self.sidebar._current_inbox_order_from_list(),
            ["acct-c", "acct-a", "acct-b"],
        )

    def test_move_inbox_row_reorders_account_sections_and_persists(self) -> None:
        self._load_sidebar()
        with mock.patch("post.sidebar.set_sidebar_state") as set_state:
            self.sidebar._move_inbox_row("acct-c", "acct-a", after=True)

        self.assertEqual(
            self.sidebar._current_inbox_order_from_list(),
            ["acct-a", "acct-c", "acct-b"],
        )
        self.assertEqual(
            _account_section_uids(self.sidebar),
            ["acct-a", "acct-c", "acct-b"],
        )
        set_state.assert_called()
        kwargs = set_state.call_args.kwargs
        self.assertEqual(kwargs["inbox_order"], ["acct-a", "acct-c", "acct-b"])

    def test_sort_account_sections_keeps_inbox_expander_first(self) -> None:
        self._load_sidebar()
        self.sidebar._inbox_order = ["acct-b", "acct-c", "acct-a"]
        self.sidebar._sort_account_sections()

        first = self.sidebar._sidebar_box.get_first_child()
        self.assertIs(first, self.sidebar._inbox_expander)
        self.assertEqual(
            _account_section_uids(self.sidebar),
            ["acct-b", "acct-c", "acct-a"],
        )


if __name__ == "__main__":
    unittest.main()
