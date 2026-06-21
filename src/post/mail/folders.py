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


def folder_name_from_uri(uri: str | None) -> str | None:
    """Return a Camel folder full name from an EDS folder:// URI."""
    if not uri:
        return None
    uri = uri.strip()
    if uri.startswith("folder://local/"):
        return uri.removeprefix("folder://local/")
    if uri.startswith("folder://"):
        parts = uri.split("/", 3)
        return parts[3] if len(parts) > 3 else None
    return uri or None


POST_OUTBOX_FOLDER = ".post/Outbox"


def is_post_outbox_folder(full_name: str | None) -> bool:
    return full_name == POST_OUTBOX_FOLDER


def outbox_folder_dict(total: int) -> dict:
    return {
        "full_name": POST_OUTBOX_FOLDER,
        "display_name": "Outbox",
        "unread": 0,
        "total": total,
    }


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


_TYPE_DRAFTS = 12288  # Camel.FolderInfoFlags.TYPE_DRAFTS
_DRAFTS_NAME_FALLBACKS = frozenset({"drafts", "draft"})

_SYSTEM_FOLDER_TYPES: tuple[int, ...] = (
    1024,   # TYPE_INBOX
    5120,   # TYPE_SENT
    3072,   # TYPE_TRASH
    4096,   # TYPE_JUNK
    _TYPE_DRAFTS,
    11264,  # TYPE_ARCHIVE
)


def is_drafts_folder(folder: dict, *, type_mask: int = 64512) -> bool:
    """Return True when Camel marks a folder as Drafts (or name matches)."""
    if folder_matches_type(folder, _TYPE_DRAFTS, type_mask=type_mask):
        return True
    display = (folder.get("display_name") or "").strip().lower()
    full = (folder.get("full_name") or "").strip().lower()
    base = full.rsplit("/", 1)[-1]
    return display in _DRAFTS_NAME_FALLBACKS or base in _DRAFTS_NAME_FALLBACKS


def is_drafts_folder_name(
    folders: list[dict],
    folder_name: str,
    *,
    type_mask: int = 64512,
) -> bool:
    """Return True when folder_name refers to a Drafts folder."""
    for folder in folders:
        if folder.get("full_name") == folder_name:
            return is_drafts_folder(folder, type_mask=type_mask)
    base = folder_name.rsplit("/", 1)[-1].lower()
    return base in _DRAFTS_NAME_FALLBACKS


def is_system_folder(
    folder: dict,
    *,
    type_mask: int = 64512,
) -> bool:
    """Return True when Camel marks a folder with a special TYPE_* role."""
    if is_post_outbox_folder(folder.get("full_name")):
        return True
    if is_virtual_folder(folder.get("full_name")):
        return True
    return any(
        folder_matches_type(folder, folder_type, type_mask=type_mask)
        for folder_type in _SYSTEM_FOLDER_TYPES
    )


def folder_by_full_name(folders: list[dict], full_name: str | None) -> dict | None:
    if not full_name:
        return None
    return next(
        (folder for folder in folders if folder.get("full_name") == full_name),
        None,
    )


def validate_folder_display_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Folder name cannot be empty")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError("Folder name cannot contain slashes")
    return cleaned


def read_message_count(unread: int, total: int) -> int:
    if unread < 0 or total < 0:
        return 0
    return max(0, total - unread)


def account_supports_folder_crud(*, backend: str | None) -> bool:
    return backend != "spool"


def resolve_sidebar_context_menu(
    *,
    folders: list[dict],
    folder_name: str | None,
    inbox_name: str | None,
    trash_name: str | None,
    archive_name: str | None,
    unread: int,
    total: int,
    outbox_count: int,
    folder_crud_enabled: bool,
) -> dict[str, bool]:
    """Return show/enabled flags for sidebar folder context menu actions."""
    is_account = folder_name is None
    is_outbox = is_post_outbox_folder(folder_name)
    is_inbox = bool(inbox_name and folder_name == inbox_name)
    is_trash = bool(trash_name and folder_name == trash_name)
    folder = folder_by_full_name(folders, folder_name)
    protected = folder is not None and is_system_folder(folder)
    if not protected and folder_name:
        for special_name in (inbox_name, trash_name, archive_name):
            if special_name and folder_name == special_name:
                protected = True
                break
    read_count = read_message_count(unread, total)

    show_new_folder = is_account and folder_crud_enabled
    show_new_subfolder = (
        not is_account
        and not is_outbox
        and folder_crud_enabled
        and folder is not None
        and not is_virtual_folder(folder_name)
    )
    show_rename = (
        not is_account
        and not is_outbox
        and folder_crud_enabled
        and folder is not None
        and not protected
    )
    show_delete = show_rename
    show_archive_read = is_inbox and archive_name is not None
    show_send_now = is_outbox
    show_empty_trash = is_trash
    show_refresh = True

    return {
        "show_new_folder": show_new_folder,
        "enable_new_folder": show_new_folder,
        "show_new_subfolder": show_new_subfolder,
        "enable_new_subfolder": show_new_subfolder,
        "show_rename": show_rename,
        "enable_rename": show_rename,
        "show_delete": show_delete,
        "enable_delete": show_delete,
        "show_archive_read": show_archive_read,
        "enable_archive_read": show_archive_read and read_count > 0,
        "show_send_now": show_send_now,
        "enable_send_now": show_send_now and outbox_count > 0,
        "show_empty_trash": show_empty_trash,
        "enable_empty_trash": show_empty_trash and total > 0,
        "show_refresh": show_refresh,
        "enable_refresh": True,
        "read_count": read_count,
        "archive_folder": archive_name,
    }
