# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Headless probe — list accounts, folders, and Inbox subjects (no GUI)."""

from __future__ import annotations

import sys

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from post.mail import MailService


def main() -> int:
    try:
        mail = MailService.connect()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    accounts = mail.list_accounts()
    if not accounts:
        print("No mail accounts in Evolution Data Server.")
        print("Tip: add an account in Evolution or GNOME Settings → Online Accounts.")
        return 1

    print(f"Found {len(accounts)} account(s):\n")
    for account in accounts:
        print(f"  • {account.name} <{account.email}> [{account.backend}]")

    account = MailService.pick_default_account(accounts) or accounts[0]
    print(f"\nFolders for {account.name}:")
    try:
        folders = mail.list_folders(account.uid)
    except GLib.Error as exc:
        print(f"  Could not connect: {exc.message}")
        print(
            "  Tip: for OAuth accounts check Online Accounts; "
            "for IMAP password accounts run Post and enter your password when prompted."
        )
        return 1
    for folder in folders[:20]:
        print(
            f"  • {folder.get('display_name') or folder.get('full_name')} "
            f"({folder.get('unread', '?')} unread)"
        )
    if len(folders) > 20:
        print(f"  … and {len(folders) - 20} more")

    inbox = MailService.guess_inbox(folders)
    if not inbox:
        print("\nNo INBOX found.")
        return 0

    print(f"\nLatest messages in {inbox}:")
    messages = mail.list_messages(account.uid, inbox, limit=10)
    for msg in messages:
        print(f"  • {msg.get('from', '?')[:40]:40}  {msg.get('subject', '')[:60]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
