# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Post application preferences (non-EDS settings)."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

_PREF_PATH = os.path.join(os.path.expanduser("~"), ".config", "post", "preferences.json")

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


def get_load_remote_content() -> bool:
    return bool(_load_raw().get("load_remote_content"))


def set_load_remote_content(value: bool) -> None:
    data = _load_raw()
    data["load_remote_content"] = value
    _save_raw(data)


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
