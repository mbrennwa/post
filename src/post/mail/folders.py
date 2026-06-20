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


def is_virtual_folder(full_name: str | None) -> bool:
    return bool(full_name and full_name.startswith(".#evolution/"))


# Camel.FolderInfoFlags (avoid importing Camel in headless helpers).
_FOLDER_NOSELECT = 1
_FOLDER_VIRTUAL = 32


def folder_can_contain_messages(folder: dict) -> bool:
    """False for virtual or IMAP NOSELECT folders that cannot hold messages."""
    if is_virtual_folder(folder.get("full_name")):
        return False
    flags = int(folder.get("flags") or 0)
    if flags & (_FOLDER_NOSELECT | _FOLDER_VIRTUAL):
        return False
    return True


def find_trash_folder(
    folders: list[dict],
    *,
    trash_type: int,
    type_mask: int,
) -> dict | None:
    """Return the best trash folder, preferring real IMAP folders over virtual ones."""
    name_fallbacks = frozenset({"trash", "deleted", "bin"})
    real: list[dict] = []
    virtual: list[dict] = []

    for folder in folders:
        full_name = folder.get("full_name") or ""
        display = (folder.get("display_name") or "").strip().lower()
        base = full_name.rsplit("/", 1)[-1].lower()
        matches = (
            folder_matches_type(folder, trash_type, type_mask=type_mask)
            or display in name_fallbacks
            or base in name_fallbacks
        )
        if not matches:
            continue
        if is_virtual_folder(full_name):
            virtual.append(folder)
        else:
            real.append(folder)

    if real:
        for folder in real:
            if folder_matches_type(folder, trash_type, type_mask=type_mask):
                return folder
        return real[0]

    if virtual:
        return virtual[0]

    return find_folder_by_type(
        folders,
        trash_type,
        type_mask=type_mask,
        name_fallbacks=name_fallbacks,
    )


def filter_sidebar_folders(folders: list[dict]) -> list[dict]:
    """Hide empty Camel virtual folders when a real folder exists."""
    hidden: set[str] = set()
    for label in ("trash", "junk"):
        real_exists = any(
            (folder.get("display_name") or "").strip().lower() == label
            and not is_virtual_folder(folder.get("full_name"))
            for folder in folders
        )
        if not real_exists:
            continue
        for folder in folders:
            full_name = folder.get("full_name") or ""
            if (
                is_virtual_folder(full_name)
                and (folder.get("display_name") or "").strip().lower() == label
            ):
                hidden.add(full_name)
    if not hidden:
        return folders
    return [folder for folder in folders if folder.get("full_name") not in hidden]


def resolve_move_menu_state(
    folders: list[dict],
    current_folder: str,
    *,
    archive_type: int,
    trash_type: int,
    type_mask: int,
) -> dict[str, str | bool | None]:
    archive_info = find_folder_by_type(
        folders,
        archive_type,
        type_mask=type_mask,
        name_fallbacks=frozenset({"archive", "archives"}),
    )
    trash_info = find_trash_folder(
        folders,
        trash_type=trash_type,
        type_mask=type_mask,
    )
    archive_name = archive_info.get("full_name") if archive_info else None
    trash_name = trash_info.get("full_name") if trash_info else None
    return {
        "archive_folder": archive_name,
        "trash_folder": trash_name,
        "inbox_folder": guess_inbox_name(folders),
        "can_archive": archive_name is not None and current_folder != archive_name,
        "can_trash": trash_name is not None and current_folder != trash_name,
    }


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
