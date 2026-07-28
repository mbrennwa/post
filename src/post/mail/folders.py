# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Folder display helpers (headless — safe to unit test)."""

from __future__ import annotations


def format_folder_label(
    display: str,
    unread: int,
    total: int,
    *,
    status_pending: bool = False,
) -> str:
    if unread >= 0 and total >= 0:
        return f"{display} ({unread}/{total})"
    if total >= 0:
        return f"{display} ({total})"
    if unread >= 0:
        return f"{display} ({unread})"
    if status_pending:
        return f"{display} (working…)"
    return display


def resolve_folder_display_name(
    *,
    folder_name: str,
    display_name: str | None = None,
    inbox_name: str | None = None,
    account_label: str | None = None,
    is_outbox: bool = False,
) -> str:
    """Return a user-facing folder label for status messages."""
    if is_outbox:
        return "Outbox"
    if inbox_name and folder_name == inbox_name and account_label:
        return f"{account_label} Inbox"
    return display_name or folder_name


def format_folder_refresh_start(display_name: str) -> str:
    return f"Refreshing {display_name}…"


def format_folder_refresh_done(display_name: str, unread: int, total: int) -> str:
    if total >= 0 and unread >= 0:
        return f"Refreshed {display_name}: {total} messages ({unread} unread)"
    if total >= 0:
        return f"Refreshed {display_name}: {total} messages"
    return f"Refreshed {display_name}"


def format_folder_refresh_error(display_name: str) -> str:
    return f"Could not refresh {display_name}"


def format_account_refresh_start(display_label: str) -> str:
    return f"Refreshing folders for {display_label}…"


def format_account_refresh_done(display_label: str, folder_count: int) -> str:
    noun = "folder" if folder_count == 1 else "folders"
    return f"Refreshed {folder_count} {noun} for {display_label}"


def format_account_refresh_error(display_label: str) -> str:
    return f"Could not refresh folders for {display_label}"


def format_startup_loading_accounts() -> str:
    return "Loading accounts…"


def format_startup_loading_folders(done: int, total: int) -> str:
    noun = "account" if total == 1 else "accounts"
    return f"Loading folders for {done} of {total} {noun}…"


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


def is_post_local_folder(full_name: str | None) -> bool:
    """True for Post-local folders (outbox queue), not on the mail server."""
    return bool(full_name and full_name.startswith(".post/"))


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


def folder_names_for_count_refresh(folders: list[dict]) -> list[str]:
    """Return full names of folders whose unread/total badges should be polled."""
    names: list[str] = []
    for folder in folders:
        full_name = folder.get("full_name")
        if not full_name:
            continue
        if is_post_local_folder(full_name):
            continue
        if not folder_can_contain_messages(folder):
            continue
        names.append(full_name)
    return names


def find_trash_folder(
    folders: list[dict],
    *,
    trash_type: int,
    type_mask: int,
) -> dict | None:
    """Return the best trash folder, preferring real IMAP folders over virtual ones."""
    name_fallbacks = frozenset(
        {
            "trash",
            "deleted",
            "bin",
            "deleted items",
            "deleted messages",
        }
    )
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


_TYPE_SENT = 5120  # Camel.FolderInfoFlags.TYPE_SENT
_TYPE_DRAFTS = 12288  # Camel.FolderInfoFlags.TYPE_DRAFTS
_DRAFTS_NAME_FALLBACKS = frozenset({"drafts", "draft"})
_SENT_NAME_FALLBACKS = frozenset({"sent", "sent mail", "sent messages"})
_JUNK_NAME_FALLBACKS = frozenset({"junk", "spam"})


def _folder_matches_name_fallbacks(
    folder: dict, fallbacks: frozenset[str]
) -> bool:
    display = (folder.get("display_name") or "").strip().lower()
    full = (folder.get("full_name") or "").strip().lower()
    base = full.rsplit("/", 1)[-1]
    return display in fallbacks or base in fallbacks

_SYSTEM_FOLDER_TYPES: tuple[int, ...] = (
    1024,   # TYPE_INBOX
    _TYPE_SENT,
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


def is_sent_folder(folder: dict, *, type_mask: int = 64512) -> bool:
    """Return True when Camel marks a folder as Sent (or name matches)."""
    if folder_matches_type(folder, _TYPE_SENT, type_mask=type_mask):
        return True
    return _folder_matches_name_fallbacks(folder, _SENT_NAME_FALLBACKS)


def is_sent_folder_name(
    folders: list[dict],
    folder_name: str,
    *,
    type_mask: int = 64512,
) -> bool:
    """Return True when folder_name refers to a Sent folder."""
    for folder in folders:
        if folder.get("full_name") == folder_name:
            return is_sent_folder(folder, type_mask=type_mask)
    base = folder_name.rsplit("/", 1)[-1].lower()
    return base in _SENT_NAME_FALLBACKS


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
    if is_drafts_folder(folder, type_mask=type_mask):
        return True
    if _folder_matches_name_fallbacks(folder, _SENT_NAME_FALLBACKS):
        return True
    if _folder_matches_name_fallbacks(folder, _JUNK_NAME_FALLBACKS):
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
    network_available: bool = True,
    account_user_online: bool = True,
    account_offline_toggle_enabled: bool = False,
    account_connect_health: str = "ok",
    is_unified_inbox: bool = False,
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

    account_effectively_online = (
        account_user_online
        and network_available
        and account_connect_health == "ok"
    )
    show_new_folder = is_account and folder_crud_enabled and account_effectively_online
    show_new_subfolder = (
        not is_account
        and not is_outbox
        and folder_crud_enabled
        and folder is not None
        and not is_virtual_folder(folder_name)
        and account_effectively_online
    )
    show_rename = (
        not is_account
        and not is_outbox
        and folder_crud_enabled
        and folder is not None
        and not protected
        and account_effectively_online
    )
    show_delete = show_rename
    show_archive_read = (
        is_inbox and archive_name is not None and account_effectively_online
    )
    show_archive_read_unflagged = show_archive_read
    show_archive_all = show_archive_read
    show_send_now = is_outbox and account_effectively_online
    show_empty_trash = is_trash and account_effectively_online
    # Refresh when the account can talk to the network. Allow retry while
    # degraded (needs_sign_in / not_connected) so GOA re-auth + Refresh works.
    show_refresh = (
        not is_unified_inbox
        and account_user_online
        and network_available
    )
    # Account online toggle: account headers and unified Inboxes rows.
    allow_online_toggle = account_offline_toggle_enabled and (
        is_account or is_unified_inbox
    )
    show_take_offline = allow_online_toggle and account_effectively_online
    show_take_online = allow_online_toggle and not account_effectively_online

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
        "show_archive_read_unflagged": show_archive_read_unflagged,
        "enable_archive_read_unflagged": show_archive_read_unflagged and read_count > 0,
        "show_archive_all": show_archive_all,
        "enable_archive_all": show_archive_all and total > 0,
        "show_send_now": show_send_now,
        "enable_send_now": show_send_now and outbox_count > 0,
        "show_empty_trash": show_empty_trash,
        "enable_empty_trash": show_empty_trash and total > 0,
        "show_refresh": show_refresh,
        "enable_refresh": show_refresh,
        "show_take_offline": show_take_offline,
        "enable_take_offline": show_take_offline,
        "show_take_online": show_take_online,
        "enable_take_online": show_take_online,
        "read_count": read_count,
        "archive_folder": archive_name,
    }
