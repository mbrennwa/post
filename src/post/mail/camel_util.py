# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python helpers for Camel API values."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import logging
import re
from typing import Any


log = logging.getLogger(__name__)

_UID_B64_PREFIX = "uidb64:"
_NUMERIC_UID_RE = re.compile(r"^[1-9][0-9]*$")


class _GPtrArray(ctypes.Structure):
    _fields_ = [
        ("pdata", ctypes.POINTER(ctypes.c_void_p)),
        ("len", ctypes.c_uint),
    ]


_libcamel_lib: ctypes.CDLL | None = None
# Bound ctypes symbol for listing folder UIDs (name differs across EDS versions).
_libcamel_folder_uids: Any | None = None


def _bind_libcamel_folder_uids(lib: ctypes.CDLL) -> Any:
    """Resolve camel_folder_dup_uids (EDS ≥3.58) or camel_folder_get_uids."""
    for name in ("camel_folder_dup_uids", "camel_folder_get_uids"):
        try:
            func = getattr(lib, name)
        except AttributeError:
            continue
        func.argtypes = [ctypes.c_void_p]
        func.restype = ctypes.POINTER(_GPtrArray)
        return func
    raise OSError(
        "libcamel-1.2 has neither camel_folder_dup_uids nor camel_folder_get_uids"
    )


def _get_libcamel() -> ctypes.CDLL:
    global _libcamel_lib, _libcamel_folder_uids
    if _libcamel_lib is None:
        path = ctypes.util.find_library("camel-1.2")
        if not path:
            raise OSError("libcamel-1.2 not found")
        lib = ctypes.CDLL(path)
        _libcamel_folder_uids = _bind_libcamel_folder_uids(lib)
        lib.camel_folder_get_message_info.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        lib.camel_folder_get_message_info.restype = ctypes.c_void_p
        lib.camel_folder_search_by_expression.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.camel_folder_search_by_expression.restype = ctypes.POINTER(_GPtrArray)
        lib.camel_folder_search_free.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_GPtrArray),
        ]
        lib.camel_folder_search_free.restype = None
        _libcamel_lib = lib
    return _libcamel_lib


def _gobject_pointer(value: Any) -> ctypes.c_void_p:
    capsule = value.__gpointer__
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return ctypes.c_void_p(ctypes.pythonapi.PyCapsule_GetPointer(capsule, None))


def _decode_uid_bytes(raw: bytes) -> str | None:
    """Decode a Camel UID C string; reject obvious non-UID garbage."""
    if not raw:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return _UID_B64_PREFIX + base64.b64encode(raw).decode("ascii")
    if _NUMERIC_UID_RE.match(text):
        return text
    return None


def camel_uid_to_bytes(uid: str) -> bytes:
    if uid.startswith(_UID_B64_PREFIX):
        return base64.b64decode(uid[len(_UID_B64_PREFIX) :], validate=True)
    return uid.encode("utf-8")


def camel_uid_is_binary(uid: str) -> bool:
    return uid.startswith(_UID_B64_PREFIX)


def camel_uid_to_api(uid: str) -> str:
    """Return a GI utf8 argument for numeric UIDs (never bytes)."""
    if camel_uid_is_binary(uid):
        raise TypeError(
            "Binary Camel UIDs must use folder_get_message_info() "
            "or other camel_util folder helpers"
        )
    return uid


def _is_plausible_uid(uid: str) -> bool:
    if not uid:
        return False
    if _NUMERIC_UID_RE.match(uid):
        return True
    if uid.startswith(_UID_B64_PREFIX):
        try:
            camel_uid_to_bytes(uid)
        except (ValueError, UnicodeError):
            return False
        return True
    return False


def _wrap_camel_message_info(ptr: int) -> Any:
    import gi
    from gi import _gi

    gi.require_version("Camel", "1.2")
    from gi.repository import Camel

    return _gi.pygobject_new_full(ptr, Camel.MessageInfo.__gtype__, True, True)


def _folder_get_message_info_via_ctypes(folder: Any, uid_bytes: bytes) -> Any:
    ptr = _get_libcamel().camel_folder_get_message_info(
        _gobject_pointer(folder), uid_bytes
    )
    if not ptr:
        return None
    return _wrap_camel_message_info(ptr)


def _folder_list_uids_gi(folder: Any) -> Any:
    """Return the GI UID list (dup_uids on EDS ≥3.58, else get_uids).

    Methods are resolved on ``type(folder)`` so ``unittest.mock.Mock``
    auto-attributes do not pick the wrong branch. Test doubles should
    define ``dup_uids`` / ``get_uids`` on a real class (or use ``spec``).
    """
    if callable(getattr(type(folder), "dup_uids", None)):
        return folder.dup_uids()
    if callable(getattr(type(folder), "get_uids", None)):
        return folder.get_uids()
    raise AttributeError(
        f"{type(folder).__name__} has neither dup_uids nor get_uids"
    )


def _folder_uids_via_ctypes(folder: Any) -> list[str]:
    _get_libcamel()
    assert _libcamel_folder_uids is not None
    array = _libcamel_folder_uids(_gobject_pointer(folder))
    if not array:
        return []
    uids: list[str] = []
    for index in range(array.contents.len):
        ptr = array.contents.pdata[index]
        if not ptr:
            continue
        uid = _decode_uid_bytes(ctypes.string_at(ptr))
        if uid is not None and _is_plausible_uid(uid):
            uids.append(uid)
    return uids


def folder_get_uids(folder: Any) -> list[str]:
    """Return folder UIDs, bypassing PyGObject UTF-8 decoding when needed."""
    try:
        uids = camel_uid_list(_folder_list_uids_gi(folder))
    except UnicodeDecodeError:
        uids = _folder_uids_via_ctypes(folder)
    else:
        return uids
    if uids:
        return uids
    log.warning("Could not decode folder UIDs via GI or ctypes")
    return []


def folder_get_unread_count(folder: Any) -> int:
    """Return unread count across Camel API renames.

    Older Camel exposes ``Camel.Folder.get_unread_message_count``. EDS 3.58+
    removed it; use ``FolderSummary.get_unread_count`` (available since 3.4).
    Returns ``-1`` when the count is unavailable (Camel's historical unknown).
    """
    if callable(getattr(type(folder), "get_unread_message_count", None)):
        return int(folder.get_unread_message_count())

    # Prefer type-level summary API so MagicMock.__int__ (always 1) cannot
    # shadow a configured get_unread_message_count on test doubles.
    if callable(getattr(type(folder), "get_folder_summary", None)):
        summary = folder.get_folder_summary()
        if summary is not None and callable(
            getattr(type(summary), "get_unread_count", None)
        ):
            return int(summary.get_unread_count())

    children = getattr(folder, "_mock_children", None)
    if isinstance(children, dict) and "get_unread_message_count" in children:
        return int(folder.get_unread_message_count())
    if isinstance(children, dict) and "get_folder_summary" in children:
        summary = folder.get_folder_summary()
        summary_children = getattr(summary, "_mock_children", None)
        if (
            summary is not None
            and isinstance(summary_children, dict)
            and "get_unread_count" in summary_children
        ):
            return int(summary.get_unread_count())

    return -1


def folder_get_message_info(folder: Any, uid: str) -> Any:
    """Fetch CamelMessageInfo for a UID, including non-UTF-8/binary UIDs."""
    if camel_uid_is_binary(uid):
        return _folder_get_message_info_via_ctypes(folder, camel_uid_to_bytes(uid))
    return folder.get_message_info(uid)


def _uid_lookup_keys(uid: str) -> set[str]:
    keys = {uid}
    stripped = uid.lstrip("0") or "0"
    keys.add(stripped)
    return keys


def _read_ptr_array_uids(array: ctypes.POINTER(_GPtrArray) | None) -> list[str]:
    if not array:
        return []
    uids: list[str] = []
    for index in range(array.contents.len):
        ptr = array.contents.pdata[index]
        if not ptr:
            continue
        uid = _decode_uid_bytes(ctypes.string_at(ptr))
        if uid is not None:
            uids.append(uid)
    return uids


def _align_uids_to_scope(matches: list[str], scope_uids: list[str]) -> list[str]:
    if not scope_uids:
        return matches
    uid_lookup: dict[str, str] = {}
    for uid in scope_uids:
        for key in _uid_lookup_keys(uid):
            uid_lookup[key] = uid
    aligned: list[str] = []
    seen: set[str] = set()
    for uid in matches:
        index_uid = uid_lookup.get(uid)
        if index_uid is None:
            for key in _uid_lookup_keys(uid):
                index_uid = uid_lookup.get(key)
                if index_uid is not None:
                    break
        if index_uid is None or index_uid in seen:
            continue
        seen.add(index_uid)
        aligned.append(index_uid)
    return aligned


def folder_search_uids(
    folder: Any,
    expression: str,
    scope_uids: list[str],
    *,
    cancellable: Any | None = None,
) -> list[str]:
    """Run Camel folder search and return matching UID strings.

    Uses libcamel directly because PyGObject's conversion of search results
  corrupts Camel's pooled UID strings: the first search works, later searches
    return empty and Camel logs camel_pstring_free warnings.
    """
    if not scope_uids:
        return []
    if cancellable is not None and cancellable.is_cancelled():
        return []
    lib = _get_libcamel()
    folder_ptr = _gobject_pointer(folder)
    cancel_ptr = (
        _gobject_pointer(cancellable)
        if cancellable is not None
        else ctypes.c_void_p()
    )
    array = lib.camel_folder_search_by_expression(
        folder_ptr,
        expression.encode("utf-8"),
        None,
        cancel_ptr,
    )
    if cancellable is not None and cancellable.is_cancelled():
        if array:
            lib.camel_folder_search_free(folder_ptr, array)
        return []
    try:
        matches = _read_ptr_array_uids(array)
    finally:
        if array:
            lib.camel_folder_search_free(folder_ptr, array)
    aligned = _align_uids_to_scope(matches, scope_uids)
    return aligned


def normalize_camel_uid(value: Any) -> str | None:
    """Return a stripped Camel UID string, or None if empty/invalid.

    Accepts IMAP numeric UIDs, ``uidb64:…`` binary UIDs, and opaque
    non-numeric Camel/Graph UIDs (e.g. Microsoft 365). Rejects empty,
    whitespace-only, and ``"0"``.
    """
    uid = str(value).strip()
    if not uid or uid == "0":
        return None
    if uid.startswith(_UID_B64_PREFIX):
        try:
            camel_uid_to_bytes(uid)
        except (ValueError, UnicodeError):
            return None
        return uid
    return uid


def camel_uid_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        length = value.get_length()
        return [str(value.get_nth(index)) for index in range(length)]
    except (AttributeError, TypeError):
        pass
    try:
        return [str(uid) for uid in value]
    except TypeError:
        return [str(value)]
