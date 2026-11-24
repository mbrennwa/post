# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-Python helpers for Camel API values."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import logging
import re
import threading
from typing import Any


log = logging.getLogger(__name__)

_UID_B64_PREFIX = "uidb64:"
_NUMERIC_UID_RE = re.compile(r"^[1-9][0-9]*$")
# Mis-read IMAP reply words. Opaque ids (Microsoft 365) are kept.
_IMAP_TOKEN_GARBAGE = frozenset(
    {
        "BAD",
        "BYE",
        "CAPABILITY",
        "EXISTS",
        "EXPUNGE",
        "FETCH",
        "FLAGS",
        "LIST",
        "LSUB",
        "NO",
        "OK",
        "PREAUTH",
        "QUOTA",
        "RECENT",
        "SEARCH",
        "STATUS",
        "STORE",
        "UID",
    }
)


class _GPtrArray(ctypes.Structure):
    _fields_ = [
        ("pdata", ctypes.POINTER(ctypes.c_void_p)),
        ("len", ctypes.c_uint),
    ]


class _GError(ctypes.Structure):
    _fields_ = [
        ("domain", ctypes.c_uint32),
        ("code", ctypes.c_int),
        ("message", ctypes.c_char_p),
    ]


_glib_error_free: Any | None = None


class CamelSearchError(RuntimeError):
    """A Camel folder search could not run or failed.

    Never mapped to "no matches": callers must show it as an error (#535).
    """


_libcamel_lib: ctypes.CDLL | None = None
_libcamel_lock = threading.Lock()
# Symbol groups already bound on ``_libcamel_lib``. Each group binds on first
# use and independently, so one missing symbol breaks only its own feature
# (#535). A failed bind is not cached; the next call raises again.
_libcamel_bound: set[str] = set()
# Bound ctypes symbol for listing folder UIDs (name differs across EDS versions).
_libcamel_folder_uids: Any | None = None
_glib_ptr_array_unref: Any | None = None


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


def _bind_libcamel_folder_uids_group(lib: ctypes.CDLL) -> None:
    global _libcamel_folder_uids
    _libcamel_folder_uids = _bind_libcamel_folder_uids(lib)


def _bind_libcamel_message_info_group(lib: ctypes.CDLL) -> None:
    lib.camel_folder_get_message_info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
    ]
    lib.camel_folder_get_message_info.restype = ctypes.c_void_p


def _bind_libcamel_search_group(lib: ctypes.CDLL) -> None:
    # gboolean camel_folder_search_sync (folder, expression,
    #     GPtrArray **out_uids, cancellable, GError **error)  (EDS >= 3.62)
    # The caller owns *out_uids (free with g_ptr_array_unref). The old
    # camel_folder_search_by_expression / camel_folder_search_free pair is gone.
    lib.camel_folder_search_sync.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(_GPtrArray)),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(_GError)),
    ]
    lib.camel_folder_search_sync.restype = ctypes.c_int


_LIBCAMEL_GROUPS: dict[str, Any] = {
    "folder_uids": _bind_libcamel_folder_uids_group,
    "message_info": _bind_libcamel_message_info_group,
    "search": _bind_libcamel_search_group,
}


def _get_libcamel(group: str | None = None) -> ctypes.CDLL:
    """Return libcamel with ``group`` (see ``_LIBCAMEL_GROUPS``) bound.

    With no group, only the library handle is returned. Groups bind lazily
    and independently, so a symbol missing from this EDS version raises on
    the feature that needs it and nowhere else.
    """
    global _libcamel_lib
    with _libcamel_lock:
        if _libcamel_lib is None:
            path = ctypes.util.find_library("camel-1.2")
            if not path:
                raise OSError("libcamel-1.2 not found")
            _libcamel_lib = ctypes.CDLL(path)
        lib = _libcamel_lib
        if group is not None and group not in _libcamel_bound:
            binder = _LIBCAMEL_GROUPS[group]
            try:
                binder(lib)
            except AttributeError as exc:
                raise OSError(
                    f"libcamel-1.2 lacks symbols for {group}: {exc}"
                ) from exc
            _libcamel_bound.add(group)
    return lib


def _gobject_pointer(value: Any) -> ctypes.c_void_p:
    capsule = value.__gpointer__
    ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return ctypes.c_void_p(ctypes.pythonapi.PyCapsule_GetPointer(capsule, None))


def _decode_uid_bytes(raw: bytes) -> str | None:
    """Decode a Camel UID C string; reject obvious non-UID garbage.

    Numeric IMAP UIDs and opaque ids (Microsoft 365) are both real UIDs.
    Keeping only numeric ids made Archive search return nothing.
    """
    if not raw:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return _UID_B64_PREFIX + base64.b64encode(raw).decode("ascii")
    if not text or any(ord(ch) < 33 or ord(ch) == 127 for ch in text):
        return None
    if text in _IMAP_TOKEN_GARBAGE:
        return None
    return text


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
    ptr = _get_libcamel("message_info").camel_folder_get_message_info(
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
    _get_libcamel("folder_uids")
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


def _g_error_free(error: ctypes.POINTER(_GError)) -> None:
    global _glib_error_free
    if _glib_error_free is None:
        path = ctypes.util.find_library("glib-2.0")
        if not path:
            return
        glib = ctypes.CDLL(path)
        glib.g_error_free.argtypes = [ctypes.c_void_p]
        glib.g_error_free.restype = None
        _glib_error_free = glib.g_error_free
    _glib_error_free(ctypes.cast(error, ctypes.c_void_p))


def _g_ptr_array_unref(array: Any) -> None:
    global _glib_ptr_array_unref
    if _glib_ptr_array_unref is None:
        path = ctypes.util.find_library("glib-2.0")
        if not path:
            return
        glib = ctypes.CDLL(path)
        glib.g_ptr_array_unref.argtypes = [ctypes.c_void_p]
        glib.g_ptr_array_unref.restype = None
        _glib_ptr_array_unref = glib.g_ptr_array_unref
    _glib_ptr_array_unref(ctypes.cast(array, ctypes.c_void_p))


_SEARCH_SYNC_MISSING = (
    "Camel.Folder.search_sync is not available. Post needs "
    "evolution-data-server 3.62 or newer."
)


def _folder_search_sync_via_gi(
    folder: Any, expression: str, cancellable: Any | None
) -> list[str]:
    """``camel_folder_search_sync`` through PyGObject.

    Raises ``UnicodeDecodeError`` when a UID is not valid UTF-8; the caller
    then retries through ``ctypes``, as ``folder_get_uids`` does.
    """
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    if not callable(getattr(type(folder), "search_sync", None)):
        raise CamelSearchError(_SEARCH_SYNC_MISSING)
    try:
        ok, out_uids = folder.search_sync(expression, cancellable)
    except GLib.Error as exc:
        if cancellable is not None and cancellable.is_cancelled():
            return []
        raise CamelSearchError(
            f"Camel folder search failed: {exc.message}"
        ) from exc
    if not ok:
        raise CamelSearchError("Camel folder search failed")
    uids: list[str] = []
    for value in out_uids or []:
        uid = normalize_camel_uid(value)
        if uid is not None:
            uids.append(uid)
    return uids


def _folder_search_sync_via_ctypes(
    folder: Any, expression: str, cancellable: Any | None
) -> list[str]:
    """``camel_folder_search_sync`` through ``ctypes`` (non-UTF-8 UIDs)."""
    try:
        lib = _get_libcamel("search")
    except OSError as exc:
        raise CamelSearchError(f"{_SEARCH_SYNC_MISSING} ({exc})") from exc
    folder_ptr = _gobject_pointer(folder)
    cancel_ptr = (
        _gobject_pointer(cancellable)
        if cancellable is not None
        else ctypes.c_void_p()
    )
    out = ctypes.POINTER(_GPtrArray)()
    error = ctypes.POINTER(_GError)()
    ok = lib.camel_folder_search_sync(
        folder_ptr,
        expression.encode("utf-8"),
        ctypes.byref(out),
        cancel_ptr,
        ctypes.byref(error),
    )
    try:
        if error:
            message = error.contents.message
            text = (
                message.decode("utf-8", "replace")
                if message
                else "camel folder search failed"
            )
            _g_error_free(error)
            if cancellable is not None and cancellable.is_cancelled():
                return []
            raise CamelSearchError(f"Camel folder search failed: {text}")
        if not ok:
            raise CamelSearchError("Camel folder search failed")
        return _read_ptr_array_uids(out)
    finally:
        if out:
            _g_ptr_array_unref(out)


def _folder_search_sync(
    folder: Any, expression: str, cancellable: Any | None
) -> list[str]:
    """Run Camel folder search (EDS >= 3.62) and return UID strings.

    Raises ``CamelSearchError`` on failure. An empty list means "no match",
    never "search failed".
    """
    try:
        return _folder_search_sync_via_gi(folder, expression, cancellable)
    except UnicodeDecodeError:
        return _folder_search_sync_via_ctypes(folder, expression, cancellable)


def folder_search_uids(
    folder: Any,
    expression: str,
    scope_uids: list[str],
    *,
    cancellable: Any | None = None,
) -> list[str]:
    """Run Camel folder search and return matching UID strings.

    Uses ``camel_folder_search_sync`` (EDS >= 3.62), which hands the caller
    an owned UID array; PyGObject converts it safely. Raises
    ``CamelSearchError`` when the search fails (#535).
    """
    if not scope_uids:
        return []
    if cancellable is not None and cancellable.is_cancelled():
        return []
    matches = _folder_search_sync(folder, expression, cancellable)
    if cancellable is not None and cancellable.is_cancelled():
        return []
    return _align_uids_to_scope(matches, scope_uids)


def folder_search_all_uids(
    folder: Any,
    expression: str,
    *,
    cancellable: Any | None = None,
) -> list[str]:
    """Run Camel folder search without restricting to a local UID scope (#365).

    Used to discover IMAP UIDs that are not yet in the local summary so the
    heavy-folder indexer can fetch headers toward STATUS. Returns an empty
    list when cancelled or when Camel yields nothing. Raises
    ``CamelSearchError`` when the search fails (#535).
    """
    if cancellable is not None and cancellable.is_cancelled():
        return []
    matches = _folder_search_sync(folder, expression, cancellable)
    if cancellable is not None and cancellable.is_cancelled():
        return []
    return matches


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
