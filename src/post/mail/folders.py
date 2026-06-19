# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Folder display helpers (headless — safe to unit test)."""

from __future__ import annotations


def format_folder_label(display: str, unread: int, total: int) -> str:
    if unread >= 0 and total >= 0:
        return f"{display} ({unread}/{total})"
    if total >= 0:
        return f"{display} ({total})"
    if unread >= 0:
        return f"{display} ({unread})"
    return display


def guess_inbox_name(folders: list[dict]) -> str | None:
    for folder in folders:
        name = (folder.get("full_name") or "").upper()
        if name in ("INBOX", "INBOX/"):
            return folder["full_name"]
    for folder in folders:
        display = (folder.get("display_name") or "").lower()
        if display == "inbox":
            return folder.get("full_name")
    return folders[0]["full_name"] if folders else None


def find_inbox_folder(folders: list[dict]) -> dict | None:
    inbox_name = guess_inbox_name(folders)
    if not inbox_name:
        return None
    return next(
        (folder for folder in folders if folder.get("full_name") == inbox_name),
        None,
    )


def folder_matches_type(folder: dict, folder_type: int, *, type_mask: int) -> bool:
    flags = int(folder.get("flags") or 0)
    return (flags & type_mask) == folder_type


def find_folder_by_type(
    folders: list[dict],
    folder_type: int,
    *,
    type_mask: int,
    name_fallbacks: frozenset[str] | None = None,
) -> dict | None:
    for folder in folders:
        if folder_matches_type(folder, folder_type, type_mask=type_mask):
            return folder

    if not name_fallbacks:
        return None

    for folder in folders:
        display = (folder.get("display_name") or "").strip().lower()
        full = (folder.get("full_name") or "").strip().lower()
        base = full.rsplit("/", 1)[-1]
        if display in name_fallbacks or base in name_fallbacks:
            return folder
    return None
