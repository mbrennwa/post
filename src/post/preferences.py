# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Post application preferences (non-EDS settings)."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

_PREF_PATH = os.path.join(os.path.expanduser("~"), ".config", "post", "preferences.json")


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


def get_show_evolution_local() -> bool | None:
    """Return user override for built-in local mail, or None for automatic."""
    value = _load_raw().get("show_evolution_local")
    if value is None:
        return None
    return bool(value)


def set_show_evolution_local(value: bool) -> None:
    data = _load_raw()
    data["show_evolution_local"] = value
    _save_raw(data)


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
