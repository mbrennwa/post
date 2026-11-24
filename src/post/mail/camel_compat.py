# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Check that the installed Evolution Data Server / Camel fits what Post calls.

Post needs EDS 3.62 or newer. EDS 3.62 removed ``camel_folder_search_by_expression``
and replaced it with ``camel_folder_search_sync``; before this check existed,
every search then failed quietly (#535). This module is the one place that
knows the minimum version and the Camel API Post relies on. It backs:

* the startup check in the app,
* the install-time checks (``scripts/install-deps.sh``, ``run.sh``, the
  ``.deb`` ``postinst`` and the ``.rpm`` ``%post``),
* ``tests/test_camel_contract.py``, which runs against the real library.

It imports only the standard library and PyGObject, so it also runs as a
plain script before Post itself is installed::

    python3 src/post/mail/camel_compat.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

MIN_EDS_VERSION = (3, 62, 0)
MIN_EDS_TEXT = "3.62"

# Camel API Post calls through PyGObject, grouped by class. Only calls that are
# part of the 3.62 API belong here; renamed or removed ones stay out.
REQUIRED_GI_METHODS: dict[str, tuple[str, ...]] = {
    "Folder": (
        "append_message_sync",
        "dup_uids",
        "expunge_sync",
        "get_display_name",
        "get_folder_summary",
        "get_full_name",
        "get_message_cached",
        "get_message_info",
        "get_message_sync",
        "refresh_info_sync",
        "search_sync",
        "synchronize_message_sync",
        "synchronize_sync",
    ),
    "Store": (
        "connect_sync",
        "create_folder_sync",
        "delete_folder_sync",
        "get_folder_info_sync",
        "get_folder_sync",
        "rename_folder_sync",
        "synchronize_sync",
    ),
    "OfflineStore": (
        "dup_downsync_folders",
        "set_online_sync",
    ),
    "Session": (
        "get_oauth2_access_token_sync",
        "ref_service",
    ),
    "FolderSummary": ("get_unread_count",),
}

# Camel C symbols Post binds through ctypes (``camel_util.py``, ``helpers.py``).
REQUIRED_CAMEL_SYMBOLS: tuple[str, ...] = (
    "camel_folder_dup_uids",
    "camel_folder_get_message_info",
    "camel_folder_search_sync",
)

# Symbols that must not be needed any more; their presence is not a problem,
# their absence is the reason for the version floor.
REMOVED_IN_3_62: tuple[str, ...] = (
    "camel_folder_search_by_expression",
    "camel_folder_search_free",
)


def _load_gi():
    """Return ``(Camel, EDataServer)`` or raise ``ImportError`` / ``ValueError``."""
    import gi

    gi.require_version("Camel", "1.2")
    gi.require_version("EDataServer", "1.2")
    from gi.repository import Camel, EDataServer

    return Camel, EDataServer


def installed_eds_version() -> tuple[int, int, int] | None:
    """EDS version from libedataserver, or ``None`` if it cannot be read."""
    try:
        _camel, eds = _load_gi()
        return (
            int(eds.EDS_MAJOR_VERSION),
            int(eds.EDS_MINOR_VERSION),
            int(eds.EDS_MICRO_VERSION),
        )
    except (ImportError, ValueError, AttributeError):
        return None


def _missing_gi_methods(camel) -> list[str]:
    missing: list[str] = []
    for class_name, methods in REQUIRED_GI_METHODS.items():
        cls = getattr(camel, class_name, None)
        if cls is None:
            missing.append(f"Camel.{class_name}")
            continue
        for method in methods:
            if not callable(getattr(cls, method, None)):
                missing.append(f"Camel.{class_name}.{method}")
    return missing


def _missing_camel_symbols() -> list[str]:
    path = ctypes.util.find_library("camel-1.2")
    if not path:
        return ["libcamel-1.2 (shared library not found)"]
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        return [f"libcamel-1.2 ({exc})"]
    missing: list[str] = []
    for name in REQUIRED_CAMEL_SYMBOLS:
        try:
            getattr(lib, name)
        except AttributeError:
            missing.append(name)
    return missing


def folder_info_layout_problems(camel=None) -> list[str]:
    """Compare ``helpers._CamelFolderInfoC`` with the typelib's CamelFolderInfo.

    ``helpers.py`` walks the folder tree through a hard-coded C struct, because
    PyGObject exposes ``child`` / ``next`` as plain integers. If a later EDS
    adds or reorders a field, that code would read the wrong memory without
    any error. Returns no problems when ``post.mail.helpers`` is not importable
    (this module also runs as a stand-alone script).
    """
    try:
        from post.mail.helpers import _CamelFolderInfoC
    except ImportError:
        return []
    if camel is None:
        try:
            camel, _eds = _load_gi()
        except (ImportError, ValueError):
            return []
    info = camel.FolderInfo.__info__
    problems: list[str] = []
    if ctypes.sizeof(_CamelFolderInfoC) != info.get_size():
        problems.append(
            f"CamelFolderInfo is {info.get_size()} bytes in the typelib but "
            f"Post's struct is {ctypes.sizeof(_CamelFolderInfoC)} bytes"
        )
    typelib_offsets = {
        field.get_name(): field.get_offset() for field in info.get_fields()
    }
    for name, _ctype in _CamelFolderInfoC._fields_:
        expected = typelib_offsets.get(name)
        actual = getattr(_CamelFolderInfoC, name).offset
        if expected is None:
            problems.append(f"CamelFolderInfo has no field {name!r}")
        elif expected != actual:
            problems.append(
                f"CamelFolderInfo.{name} is at offset {expected} in the "
                f"typelib but at {actual} in Post's struct"
            )
    return problems


def check_camel_compat() -> list[str]:
    """Return problems (empty when everything Post needs is present)."""
    try:
        camel, eds = _load_gi()
    except (ImportError, ValueError) as exc:
        return [
            "Evolution Data Server GObject bindings could not be loaded "
            f"({exc}). Install python3-gi, gir1.2-camel-1.2 and "
            "gir1.2-edataserver-1.2."
        ]
    problems: list[str] = []
    too_old = eds.eds_check_version(*MIN_EDS_VERSION)
    if too_old is not None:
        version = installed_eds_version()
        found = ".".join(str(part) for part in version) if version else "unknown"
        problems.append(
            f"Evolution Data Server {MIN_EDS_TEXT} or newer is required "
            f"(found {found})."
        )
    gi_missing = _missing_gi_methods(camel)
    if gi_missing:
        problems.append(
            "Camel API missing: " + ", ".join(gi_missing) + "."
        )
    symbols_missing = _missing_camel_symbols()
    if symbols_missing:
        problems.append(
            "libcamel symbols missing: " + ", ".join(symbols_missing) + "."
        )
    layout = folder_info_layout_problems(camel)
    if layout:
        problems.append("Camel struct layout changed: " + "; ".join(layout) + ".")
    return problems


def format_problems(problems: list[str]) -> str:
    return "\n".join(f"- {problem}" for problem in problems)


def main() -> int:
    """Exit 0 when compatible; otherwise print the problems to stderr."""
    problems = check_camel_compat()
    if not problems:
        return 0
    print(
        f"post: this system's Evolution Data Server is not compatible "
        f"(Post needs {MIN_EDS_TEXT} or newer):\n{format_problems(problems)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
