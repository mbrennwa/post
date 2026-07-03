# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Post application preferences (non-EDS settings)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Literal

_PREF_PATH = os.path.join(os.path.expanduser("~"), ".config", "post", "preferences.json")

MessageAppearance = Literal["adapt_text", "adapt_background", "accept_sender"]
OfflineBodySyncMode = Literal["off", "last_month", "last_year", "all"]

OFFLINE_BODY_SYNC_OFF: OfflineBodySyncMode = "off"
OFFLINE_BODY_SYNC_LAST_MONTH: OfflineBodySyncMode = "last_month"
OFFLINE_BODY_SYNC_LAST_YEAR: OfflineBodySyncMode = "last_year"
OFFLINE_BODY_SYNC_ALL: OfflineBodySyncMode = "all"

_OFFLINE_BODY_SYNC_VALUES: frozenset[str] = frozenset(
    {
        OFFLINE_BODY_SYNC_OFF,
        OFFLINE_BODY_SYNC_LAST_MONTH,
        OFFLINE_BODY_SYNC_LAST_YEAR,
        OFFLINE_BODY_SYNC_ALL,
    }
)

MESSAGE_APPEARANCE_ADAPT_TEXT: MessageAppearance = "adapt_text"
MESSAGE_APPEARANCE_ADAPT_BACKGROUND: MessageAppearance = "adapt_background"
MESSAGE_APPEARANCE_ACCEPT_SENDER: MessageAppearance = "accept_sender"

_MESSAGE_APPEARANCE_VALUES: frozenset[str] = frozenset(
    {
        MESSAGE_APPEARANCE_ADAPT_TEXT,
        MESSAGE_APPEARANCE_ADAPT_BACKGROUND,
        MESSAGE_APPEARANCE_ACCEPT_SENDER,
    }
)

_DEFAULT_WINDOW_WIDTH = 1100
_DEFAULT_WINDOW_HEIGHT = 720


def _load_raw() -> dict[str, Any]:
    try:
        with open(_PREF_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_raw(data: dict[str, Any]) -> None:
    directory = os.path.dirname(_PREF_PATH)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, _PREF_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_show_evolution_local() -> bool | None:
    """Return user override for built-in local mail, or None for automatic."""
    value = _load_raw().get("show_evolution_local")
    if value is None:
        return None
    return bool(value)


SearchScopeKind = Literal["folder", "all", "account"]

SEARCH_SCOPE_FOLDER: SearchScopeKind = "folder"
SEARCH_SCOPE_ALL: SearchScopeKind = "all"
SEARCH_SCOPE_ACCOUNT: SearchScopeKind = "account"

_SEARCH_SCOPE_KINDS: frozenset[str] = frozenset(
    {SEARCH_SCOPE_FOLDER, SEARCH_SCOPE_ALL, SEARCH_SCOPE_ACCOUNT}
)


@dataclass(frozen=True)
class SearchScope:
    kind: SearchScopeKind
    account_uid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SEARCH_SCOPE_KINDS:
            raise ValueError(f"Invalid search scope kind: {self.kind!r}")
        if self.kind == SEARCH_SCOPE_ACCOUNT:
            if not self.account_uid:
                raise ValueError("account_uid required for account search scope")
        elif self.account_uid is not None:
            raise ValueError("account_uid only valid for account search scope")


def _search_scope_to_storage(scope: SearchScope) -> str:
    if scope.kind == SEARCH_SCOPE_FOLDER:
        return SEARCH_SCOPE_FOLDER
    if scope.kind == SEARCH_SCOPE_ALL:
        return SEARCH_SCOPE_ALL
    assert scope.account_uid is not None
    return f"{SEARCH_SCOPE_ACCOUNT}:{scope.account_uid}"


def _search_scope_from_storage(value: str) -> SearchScope | None:
    if value == SEARCH_SCOPE_FOLDER:
        return SearchScope(SEARCH_SCOPE_FOLDER)
    if value == SEARCH_SCOPE_ALL:
        return SearchScope(SEARCH_SCOPE_ALL)
    if value.startswith(f"{SEARCH_SCOPE_ACCOUNT}:"):
        account_uid = value[len(SEARCH_SCOPE_ACCOUNT) + 1 :]
        if account_uid:
            return SearchScope(SEARCH_SCOPE_ACCOUNT, account_uid=account_uid)
    return None


def get_search_scope() -> SearchScope:
    data = _load_raw()
    stored = data.get("search_scope")
    if isinstance(stored, str):
        scope = _search_scope_from_storage(stored)
        if scope is not None:
            return scope
    if bool(data.get("search_all_mail")):
        return SearchScope(SEARCH_SCOPE_ALL)
    return SearchScope(SEARCH_SCOPE_FOLDER)


def set_search_scope(scope: SearchScope) -> None:
    data = _load_raw()
    data["search_scope"] = _search_scope_to_storage(scope)
    data.pop("search_all_mail", None)
    _save_raw(data)


def get_load_remote_content() -> bool:
    return bool(_load_raw().get("load_remote_content"))


def set_load_remote_content(value: bool) -> None:
    data = _load_raw()
    data["load_remote_content"] = value
    _save_raw(data)


def get_message_appearance() -> MessageAppearance:
    value = _load_raw().get("message_appearance")
    if isinstance(value, str) and value in _MESSAGE_APPEARANCE_VALUES:
        return value  # type: ignore[return-value]
    return MESSAGE_APPEARANCE_ADAPT_TEXT


def set_message_appearance(value: MessageAppearance) -> None:
    if value not in _MESSAGE_APPEARANCE_VALUES:
        raise ValueError(f"Invalid message appearance: {value!r}")
    data = _load_raw()
    data["message_appearance"] = value
    _save_raw(data)


REMOTE_SYNC_ACCOUNT_BACKENDS: frozenset[str] = frozenset(
    {"imap", "imapx", "ews", "microsoft365", "pop3"}
)


def account_supports_user_offline(backend: str | None) -> bool:
    """Return whether the user may toggle online/offline for this account."""
    return backend in REMOTE_SYNC_ACCOUNT_BACKENDS if backend else False


def _account_user_online_raw() -> dict[str, Any]:
    raw = _load_raw().get("account_user_online")
    if isinstance(raw, dict):
        return raw
    return {}


def get_account_user_online(account_uid: str) -> bool:
    """Return per-account user online state (default True)."""
    value = _account_user_online_raw().get(account_uid)
    if value is None:
        return True
    return bool(value)


def set_account_user_online(account_uid: str, online: bool) -> None:
    data = _load_raw()
    modes = dict(_account_user_online_raw())
    if online:
        modes.pop(account_uid, None)
    else:
        modes[account_uid] = False
    if modes:
        data["account_user_online"] = modes
    else:
        data.pop("account_user_online", None)
    _save_raw(data)


SEND_DELAY_OFF = 0
SEND_DELAY_PRESETS: tuple[int, ...] = (0, 5, 10, 30, 60, 120, 300)
_SEND_DELAY_LABELS: tuple[str, ...] = (
    "Off (send immediately)",
    "5 seconds",
    "10 seconds",
    "30 seconds",
    "1 minute",
    "2 minutes",
    "5 minutes",
)


def get_send_delay_seconds() -> int:
    value = _load_raw().get("send_delay_seconds", SEND_DELAY_OFF)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return SEND_DELAY_OFF
    if seconds not in SEND_DELAY_PRESETS:
        return SEND_DELAY_OFF
    return seconds


def set_send_delay_seconds(seconds: int) -> None:
    if seconds not in SEND_DELAY_PRESETS:
        raise ValueError(f"Invalid send delay: {seconds!r}")
    data = _load_raw()
    if seconds == SEND_DELAY_OFF:
        data.pop("send_delay_seconds", None)
    else:
        data["send_delay_seconds"] = seconds
    _save_raw(data)


def send_delay_label(seconds: int) -> str:
    try:
        index = SEND_DELAY_PRESETS.index(seconds)
    except ValueError:
        return _SEND_DELAY_LABELS[0]
    return _SEND_DELAY_LABELS[index]


def format_send_delay_status(seconds: int) -> str:
    if seconds <= 0:
        return "Message sent"
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"Message will be sent in {seconds} {unit}"
    minutes = seconds // 60
    unit = "minute" if minutes == 1 else "minutes"
    return f"Message will be sent in {minutes} {unit}"


def _offline_body_sync_raw() -> dict[str, Any]:
    raw = _load_raw().get("offline_body_sync")
    if isinstance(raw, dict):
        return raw
    return {}


def get_account_offline_body_sync(account_uid: str) -> OfflineBodySyncMode:
    value = _offline_body_sync_raw().get(account_uid, OFFLINE_BODY_SYNC_OFF)
    if isinstance(value, str) and value in _OFFLINE_BODY_SYNC_VALUES:
        return value  # type: ignore[return-value]
    return OFFLINE_BODY_SYNC_OFF


def set_account_offline_body_sync(
    account_uid: str, mode: OfflineBodySyncMode
) -> None:
    if mode not in _OFFLINE_BODY_SYNC_VALUES:
        raise ValueError(f"Invalid offline body sync mode: {mode!r}")
    data = _load_raw()
    modes = _offline_body_sync_raw()
    if mode == OFFLINE_BODY_SYNC_OFF:
        modes.pop(account_uid, None)
    else:
        modes[account_uid] = mode
    data["offline_body_sync"] = modes
    _save_raw(data)


def get_all_offline_body_sync_modes() -> dict[str, OfflineBodySyncMode]:
    modes: dict[str, OfflineBodySyncMode] = {}
    for uid, value in _offline_body_sync_raw().items():
        if isinstance(uid, str) and isinstance(value, str) and value in _OFFLINE_BODY_SYNC_VALUES:
            modes[uid] = value  # type: ignore[assignment]
    return modes


def set_offline_body_sync_prompt_seen(seen: bool = True) -> None:
    data = _load_raw()
    data["offline_body_sync_prompt_seen"] = bool(seen)
    _save_raw(data)


def get_offline_body_sync_prompt_seen() -> bool:
    return bool(_load_raw().get("offline_body_sync_prompt_seen"))


def set_offline_body_sync_prompt_declined(declined: bool = True) -> None:
    """Remember that the user dismissed the first-run offline sync dialog."""
    data = _load_raw()
    data["offline_body_sync_prompt_declined"] = bool(declined)
    _save_raw(data)


def get_offline_body_sync_prompt_declined() -> bool:
    return bool(_load_raw().get("offline_body_sync_prompt_declined"))


def should_show_offline_body_sync_prompt(remote_account_uids: list[str]) -> bool:
    """Return whether the first-run offline body sync dialog should appear."""
    if get_offline_body_sync_prompt_declined():
        return False
    if not remote_account_uids:
        return False
    return any(
        get_account_offline_body_sync(uid) == OFFLINE_BODY_SYNC_OFF
        for uid in remote_account_uids
    )


def set_show_evolution_local(value: bool) -> None:
    data = _load_raw()
    data["show_evolution_local"] = value
    _save_raw(data)


def get_window_state() -> dict[str, int | bool]:
    """Return persisted main-window geometry."""
    raw = _load_raw().get("window")
    if not isinstance(raw, dict):
        return {
            "width": _DEFAULT_WINDOW_WIDTH,
            "height": _DEFAULT_WINDOW_HEIGHT,
            "maximized": False,
        }

    width = raw.get("width", _DEFAULT_WINDOW_WIDTH)
    height = raw.get("height", _DEFAULT_WINDOW_HEIGHT)
    maximized = bool(raw.get("maximized", False))
    try:
        width = max(400, int(width))
        height = max(300, int(height))
    except (TypeError, ValueError):
        width = _DEFAULT_WINDOW_WIDTH
        height = _DEFAULT_WINDOW_HEIGHT
    return {"width": width, "height": height, "maximized": maximized}


def set_window_state(*, width: int, height: int, maximized: bool) -> None:
    data = _load_raw()
    data["window"] = {
        "width": max(400, int(width)),
        "height": max(300, int(height)),
        "maximized": bool(maximized),
    }
    _save_raw(data)


def get_sidebar_state() -> dict[str, Any]:
    """Return persisted sidebar expand/collapse and active folder."""
    raw = _load_raw().get("sidebar")
    if not isinstance(raw, dict):
        return {
            "inbox_expanded": True,
            "accounts": {},
            "active_folder": None,
            "active_message_uid": None,
            "inbox_order": [],
        }

    inbox_expanded = bool(raw.get("inbox_expanded", True))
    accounts_raw = raw.get("accounts")
    accounts: dict[str, bool] = {}
    if isinstance(accounts_raw, dict):
        for uid, expanded in accounts_raw.items():
            if isinstance(uid, str):
                accounts[uid] = bool(expanded)

    active_folder: tuple[str, str] | None = None
    active_message_uid: str | None = None
    active_raw = raw.get("active_folder")
    if isinstance(active_raw, dict):
        account_uid = active_raw.get("account_uid")
        folder_name = active_raw.get("folder_name")
        if isinstance(account_uid, str) and isinstance(folder_name, str):
            active_folder = (account_uid, folder_name)
        message_uid = active_raw.get("message_uid")
        if isinstance(message_uid, str):
            active_message_uid = message_uid

    inbox_order: list[str] = []
    inbox_order_raw = raw.get("inbox_order")
    if isinstance(inbox_order_raw, list):
        inbox_order = [uid for uid in inbox_order_raw if isinstance(uid, str)]

    return {
        "inbox_expanded": inbox_expanded,
        "accounts": accounts,
        "active_folder": active_folder,
        "active_message_uid": active_message_uid,
        "inbox_order": inbox_order,
    }


def set_sidebar_state(
    *,
    inbox_expanded: bool,
    accounts: dict[str, bool],
    active_folder: tuple[str, str] | None,
    inbox_order: list[str] | None = None,
    active_message_uid: str | None | object = ...,
) -> None:
    data = _load_raw()
    sidebar: dict[str, Any] = {
        "inbox_expanded": bool(inbox_expanded),
        "accounts": {uid: bool(expanded) for uid, expanded in accounts.items()},
    }
    if active_folder is not None:
        account_uid, folder_name = active_folder
        folder_dict: dict[str, str] = {
            "account_uid": account_uid,
            "folder_name": folder_name,
        }
        if active_message_uid is not ...:
            if active_message_uid is not None:
                folder_dict["message_uid"] = str(active_message_uid)
        else:
            existing = data.get("sidebar")
            if isinstance(existing, dict):
                existing_folder = existing.get("active_folder")
                if (
                    isinstance(existing_folder, dict)
                    and existing_folder.get("account_uid") == account_uid
                    and existing_folder.get("folder_name") == folder_name
                    and isinstance(existing_folder.get("message_uid"), str)
                ):
                    folder_dict["message_uid"] = existing_folder["message_uid"]
        sidebar["active_folder"] = folder_dict
    else:
        sidebar["active_folder"] = None
    if inbox_order is not None:
        sidebar["inbox_order"] = list(inbox_order)
    elif isinstance(data.get("sidebar"), dict):
        existing_order = data["sidebar"].get("inbox_order")
        if isinstance(existing_order, list):
            sidebar["inbox_order"] = [
                uid for uid in existing_order if isinstance(uid, str)
            ]
    data["sidebar"] = sidebar
    _save_raw(data)


def set_active_message_uid(message_uid: str | None) -> None:
    """Update the persisted selected message for the current active folder."""
    data = _load_raw()
    sidebar = data.get("sidebar")
    if not isinstance(sidebar, dict):
        return
    active = sidebar.get("active_folder")
    if not isinstance(active, dict):
        return
    if message_uid is None:
        active.pop("message_uid", None)
    else:
        active["message_uid"] = message_uid
    sidebar["active_folder"] = active
    data["sidebar"] = sidebar
    _save_raw(data)


def resolve_inbox_display_order(saved: list[str], present: list[str]) -> list[str]:
    """Return display order for loaded accounts without dropping unloaded ones."""
    result = [uid for uid in saved if uid in present]
    for uid in present:
        if uid not in result:
            result.append(uid)
    return result


def register_inbox_accounts(saved: list[str], present: list[str]) -> list[str]:
    """Append newly discovered account UIDs to the saved inbox order."""
    updated = list(saved)
    for uid in present:
        if uid not in updated:
            updated.append(uid)
    return updated


def get_account_signatures() -> dict[str, str]:
    """Return per-account compose signatures keyed by account UID."""
    from post.mail.compose import normalize_signature_text

    raw = _load_raw().get("account_signatures")
    if not isinstance(raw, dict):
        return {}
    signatures: dict[str, str] = {}
    for uid, text in raw.items():
        if not isinstance(uid, str) or not isinstance(text, str):
            continue
        normalized = normalize_signature_text(text)
        if normalized:
            signatures[uid] = normalized
    return signatures


def get_account_signature(account_uid: str) -> str:
    return get_account_signatures().get(account_uid, "")


def set_account_signature(account_uid: str, signature: str) -> None:
    from post.mail.compose import normalize_signature_text

    data = _load_raw()
    signatures_raw = data.get("account_signatures")
    signatures: dict[str, str] = {}
    if isinstance(signatures_raw, dict):
        for uid, text in signatures_raw.items():
            if not isinstance(uid, str) or not isinstance(text, str):
                continue
            normalized = normalize_signature_text(text)
            if normalized:
                signatures[uid] = normalized
    normalized = normalize_signature_text(signature)
    if normalized:
        signatures[account_uid] = normalized
    else:
        signatures.pop(account_uid, None)
    data["account_signatures"] = signatures
    _save_raw(data)
