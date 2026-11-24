# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ctypes
import os
import unittest
from contextlib import ExitStack
from unittest import mock

from post.mail import camel_util

from post.mail.camel_util import (
    _UID_B64_PREFIX,
    CamelSearchError,
    _GError,
    _GPtrArray,
    _decode_uid_bytes,
    _folder_search_sync_via_ctypes,
    _read_ptr_array_uids,
    camel_uid_to_api,
    camel_uid_to_bytes,
    folder_get_message_info,
    folder_get_uids,
    folder_get_unread_count,
    folder_search_all_uids,
    folder_search_uids,
)


class _LegacyFolder:
    """Stub with pre-3.58 Camel Folder UID / unread APIs."""

    def __init__(self, uids=None, unread=0, *, fail_utf8: bool = False):
        self._uids = uids if uids is not None else ["1", "2"]
        self._unread = unread
        self._fail_utf8 = fail_utf8

    def get_uids(self):
        if self._fail_utf8:
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte"
            )
        return list(self._uids)

    def get_unread_message_count(self):
        return self._unread


class _ModernFolder:
    """Stub with EDS ≥3.58 Camel Folder UID / unread APIs."""

    def __init__(self, uids=None, unread=0, *, fail_utf8: bool = False):
        self._uids = uids if uids is not None else ["9", "10"]
        self._unread = unread
        self._fail_utf8 = fail_utf8

    def dup_uids(self):
        if self._fail_utf8:
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte"
            )
        return list(self._uids)

    def get_folder_summary(self):
        unread = self._unread

        class _Summary:
            def get_unread_count(self_inner):
                return unread

        return _Summary()


class CamelUidEncodingTests(unittest.TestCase):
    def test_ascii_uid_round_trip(self) -> None:
        self.assertEqual(_decode_uid_bytes(b"12345"), "12345")
        self.assertEqual(camel_uid_to_api("12345"), "12345")
        self.assertEqual(camel_uid_to_bytes("12345"), b"12345")

    def test_binary_uid_uses_base64_prefix(self) -> None:
        raw = b"\xa0\xff\x90"
        uid = _decode_uid_bytes(raw)
        self.assertIsNotNone(uid)
        assert uid is not None
        self.assertTrue(uid.startswith(_UID_B64_PREFIX))
        self.assertEqual(camel_uid_to_bytes(uid), raw)
        with self.assertRaises(TypeError):
            camel_uid_to_api(uid)

    def test_rejects_imap_token_garbage(self) -> None:
        self.assertIsNone(_decode_uid_bytes(b"LIST"))
        self.assertIsNone(_decode_uid_bytes(b"QUOTA"))
        self.assertIsNone(_decode_uid_bytes(b""))
        self.assertIsNone(_decode_uid_bytes(b"has space"))

    def test_keeps_opaque_ascii_uids(self) -> None:
        opaque = b"A" * 40 + b"xyz"
        self.assertEqual(_decode_uid_bytes(opaque), opaque.decode("ascii"))


class FolderGetUidsTests(unittest.TestCase):
    def test_uses_get_uids_when_utf8_safe(self) -> None:
        self.assertEqual(folder_get_uids(_LegacyFolder(["1", "2"])), ["1", "2"])

    def test_uses_dup_uids_on_modern_camel(self) -> None:
        self.assertEqual(folder_get_uids(_ModernFolder(["9", "10"])), ["9", "10"])

    def test_falls_back_when_get_uids_is_not_utf8(self) -> None:
        folder = _LegacyFolder(fail_utf8=True)
        with mock.patch(
            "post.mail.camel_util._folder_uids_via_ctypes",
            return_value=[f"{_UID_B64_PREFIX}ov8="],
        ) as fallback:
            self.assertEqual(folder_get_uids(folder), [f"{_UID_B64_PREFIX}ov8="])
        fallback.assert_called_once_with(folder)

    def test_falls_back_when_dup_uids_is_not_utf8(self) -> None:
        folder = _ModernFolder(fail_utf8=True)
        with mock.patch(
            "post.mail.camel_util._folder_uids_via_ctypes",
            return_value=[f"{_UID_B64_PREFIX}ov8="],
        ) as fallback:
            self.assertEqual(folder_get_uids(folder), [f"{_UID_B64_PREFIX}ov8="])
        fallback.assert_called_once_with(folder)


class FolderGetUnreadCountTests(unittest.TestCase):
    def test_legacy_get_unread_message_count(self) -> None:
        self.assertEqual(folder_get_unread_count(_LegacyFolder(unread=3)), 3)

    def test_modern_folder_summary_unread(self) -> None:
        self.assertEqual(folder_get_unread_count(_ModernFolder(unread=5)), 5)

    def test_mock_legacy_return_value(self) -> None:
        folder = mock.Mock()
        folder.get_unread_message_count.return_value = 4
        self.assertEqual(folder_get_unread_count(folder), 4)

    def test_magicmock_legacy_return_value(self) -> None:
        folder = mock.MagicMock()
        folder.get_unread_message_count.return_value = 0
        self.assertEqual(folder_get_unread_count(folder), 0)

    def test_unconfigured_mock_returns_unknown(self) -> None:
        self.assertEqual(folder_get_unread_count(mock.Mock()), -1)
        self.assertEqual(folder_get_unread_count(mock.MagicMock()), -1)


class FolderGetMessageInfoTests(unittest.TestCase):
    def test_uses_ctypes_for_binary_uid(self) -> None:
        folder = mock.Mock()
        uid = f"{_UID_B64_PREFIX}kKA="
        info = object()
        with mock.patch(
            "post.mail.camel_util._folder_get_message_info_via_ctypes",
            return_value=info,
        ) as lookup:
            self.assertIs(folder_get_message_info(folder, uid), info)
        lookup.assert_called_once_with(folder, b"\x90\xa0")
        folder.get_message_info.assert_not_called()

    def test_uses_gi_for_numeric_uid(self) -> None:
        folder = mock.Mock()
        folder_get_message_info(folder, "12345")
        folder.get_message_info.assert_called_once_with("12345")


class _SearchFolder:
    """Folder stub with the EDS >= 3.62 ``search_sync`` API.

    ``search_sync`` is looked up on the class, as the production code does, so
    ``mock.Mock`` auto-attributes cannot stand in for a missing method.
    """

    def __init__(
        self,
        uids=None,
        *,
        ok: bool = True,
        error: Exception | None = None,
        fail_utf8: bool = False,
    ):
        self._uids = uids if uids is not None else []
        self._ok = ok
        self._error = error
        self._fail_utf8 = fail_utf8
        self.calls: list[tuple] = []

    def search_sync(self, expression, cancellable=None):
        self.calls.append((expression, cancellable))
        if self._fail_utf8:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        if self._error is not None:
            raise self._error
        return self._ok, list(self._uids)


class _FolderWithoutSearchSync:
    """Stand-in for a Camel folder from EDS < 3.62."""

    def search_by_expression(self, expression, cancellable=None):  # pragma: no cover
        raise AssertionError("must not be called")


def _glib_error(message: str = "search blew up"):
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return GLib.Error.new_literal(GLib.quark_from_string("test"), message, 1)


class FolderSearchUidsTests(unittest.TestCase):
    EXPRESSION = '(match-all (header-contains "Subject" "invoice"))'

    def test_returns_search_sync_hits_in_scope(self) -> None:
        folder = _SearchFolder(["1"])

        result = folder_search_uids(folder, self.EXPRESSION, ["1", "2"])

        self.assertEqual(result, ["1"])
        self.assertEqual(folder.calls, [(self.EXPRESSION, None)])

    def test_maps_numeric_match_to_index_uid(self) -> None:
        folder = _SearchFolder(["42"])

        result = folder_search_uids(
            folder,
            '(match-all (header-contains "Subject" "Core"))',
            ["0042", "99"],
        )

        self.assertEqual(result, ["0042"])

    def test_passes_cancellable(self) -> None:
        folder = _SearchFolder(["1"])
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = False

        result = folder_search_uids(
            folder, self.EXPRESSION, ["1"], cancellable=cancellable
        )

        self.assertEqual(result, ["1"])
        self.assertIs(folder.calls[0][1], cancellable)

    def test_skips_binary_uids_in_scope_list(self) -> None:
        folder = _SearchFolder(["2"])
        binary_uid = f"{_UID_B64_PREFIX}kKA="

        result = folder_search_uids(folder, self.EXPRESSION, [binary_uid, "2"])

        self.assertEqual(result, ["2"])

    def test_opaque_microsoft_365_ids_round_trip(self) -> None:
        opaque = "AAMkAExampleOpaqueGraphMessageId0123456789abcdef="
        folder = _SearchFolder([opaque])

        result = folder_search_uids(folder, self.EXPRESSION, [opaque, "x"])

        self.assertEqual(result, [opaque])

    def test_empty_scope_returns_empty_without_search(self) -> None:
        folder = _SearchFolder(["1"])

        self.assertEqual(folder_search_uids(folder, "(match-all #t)", []), [])
        self.assertEqual(folder.calls, [])

    def test_cancelled_before_search_returns_empty_without_search(self) -> None:
        folder = _SearchFolder(["1"])
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = True

        result = folder_search_uids(
            folder, self.EXPRESSION, ["1"], cancellable=cancellable
        )

        self.assertEqual(result, [])
        self.assertEqual(folder.calls, [])

    def test_search_error_raises_instead_of_returning_no_matches(self) -> None:
        folder = _SearchFolder(error=_glib_error("index is corrupt"))

        with self.assertRaises(CamelSearchError) as ctx:
            folder_search_uids(folder, self.EXPRESSION, ["1"])

        self.assertIn("index is corrupt", str(ctx.exception))

    def test_search_returning_false_raises(self) -> None:
        folder = _SearchFolder(ok=False)

        with self.assertRaises(CamelSearchError):
            folder_search_uids(folder, self.EXPRESSION, ["1"])

    def test_error_while_cancelled_returns_empty(self) -> None:
        folder = _SearchFolder(error=_glib_error("Operation was cancelled"))
        cancellable = mock.Mock()
        # Before the search: no. After the error, and again afterwards: yes.
        cancellable.is_cancelled.side_effect = [False, True, True]

        result = folder_search_uids(
            folder, self.EXPRESSION, ["1"], cancellable=cancellable
        )

        self.assertEqual(result, [])

    def test_folder_without_search_sync_names_the_version_floor(self) -> None:
        # A bare ``mock.Mock`` would accept any attribute; this class does not.
        with self.assertRaises(CamelSearchError) as ctx:
            folder_search_uids(_FolderWithoutSearchSync(), self.EXPRESSION, ["1"])

        self.assertIn("3.62", str(ctx.exception))

    def test_mock_folder_is_not_mistaken_for_a_3_62_folder(self) -> None:
        with self.assertRaises(CamelSearchError):
            folder_search_uids(mock.Mock(), self.EXPRESSION, ["1"])

    def test_falls_back_to_ctypes_when_a_uid_is_not_utf8(self) -> None:
        folder = _SearchFolder(fail_utf8=True)

        with mock.patch(
            "post.mail.camel_util._folder_search_sync_via_ctypes",
            return_value=["7", f"{_UID_B64_PREFIX}ov8="],
        ) as fallback:
            result = folder_search_uids(
                folder, self.EXPRESSION, ["7", f"{_UID_B64_PREFIX}ov8="]
            )

        self.assertEqual(result, ["7", f"{_UID_B64_PREFIX}ov8="])
        fallback.assert_called_once_with(folder, self.EXPRESSION, None)


class FolderSearchAllUidsTests(unittest.TestCase):
    def test_does_not_restrict_to_local_uid_scope(self) -> None:
        folder = _SearchFolder(["1", "99"])

        result = folder_search_all_uids(folder, "(match-all #t)")

        self.assertEqual(result, ["1", "99"])

    def test_cancelled_returns_empty(self) -> None:
        folder = _SearchFolder(["1"])
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = True

        result = folder_search_all_uids(
            folder, "(match-all #t)", cancellable=cancellable
        )

        self.assertEqual(result, [])
        self.assertEqual(folder.calls, [])

    def test_search_error_raises(self) -> None:
        folder = _SearchFolder(error=_glib_error())

        with self.assertRaises(CamelSearchError):
            folder_search_all_uids(folder, "(match-all #t)")


class _FakeCDLL:
    """ctypes library stand-in whose attribute lookup fails like a real one.

    ``mock.Mock`` hides a missing symbol; a real ``CDLL`` raises
    ``AttributeError: undefined symbol``. This does the latter.
    """

    def __init__(self, symbols: dict):
        self._symbols = dict(symbols)

    def __getattr__(self, name):
        try:
            return self._symbols[name]
        except KeyError:
            raise AttributeError(f"undefined symbol: {name}") from None


class _Symbol:
    def __init__(self, func=None):
        self.func = func
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.func(*args)


def _fresh_binding_state():
    """Patch the module-level library cache so tests do not share it."""
    return mock.patch.multiple(
        "post.mail.camel_util",
        _libcamel_lib=None,
        _libcamel_bound=set(),
        _libcamel_folder_uids=None,
    )


class LazyLibcamelBindingTests(unittest.TestCase):
    def _patch_library(self, fake: _FakeCDLL):
        return (
            mock.patch(
                "post.mail.camel_util.ctypes.util.find_library",
                return_value="libcamel-fake.so",
            ),
            mock.patch("post.mail.camel_util.ctypes.CDLL", return_value=fake),
        )

    def test_missing_search_symbol_does_not_break_message_info(self) -> None:
        # EDS < 3.62 style library: message info yes, camel_folder_search_sync no.
        fake = _FakeCDLL({"camel_folder_get_message_info": _Symbol()})
        find, cdll = self._patch_library(fake)
        with _fresh_binding_state(), find, cdll:
            with self.assertRaises(OSError) as ctx:
                camel_util._get_libcamel("search")
            self.assertIn("camel_folder_search_sync", str(ctx.exception))
            # Independent: the message-info group still binds.
            lib = camel_util._get_libcamel("message_info")
            self.assertIs(lib, fake)
            # And the search group keeps failing, it is not cached as bound.
            with self.assertRaises(OSError):
                camel_util._get_libcamel("search")

    def test_search_group_binds_camel_folder_search_sync(self) -> None:
        search = _Symbol()
        fake = _FakeCDLL({"camel_folder_search_sync": search})
        find, cdll = self._patch_library(fake)
        with _fresh_binding_state(), find, cdll:
            camel_util._get_libcamel("search")
        self.assertEqual(len(search.argtypes), 5)
        self.assertEqual(search.restype, ctypes.c_int)

    def test_removed_symbols_are_not_required(self) -> None:
        fake = _FakeCDLL({"camel_folder_search_sync": _Symbol()})
        find, cdll = self._patch_library(fake)
        with _fresh_binding_state(), find, cdll:
            camel_util._get_libcamel("search")
        self.assertFalse(hasattr(fake, "camel_folder_search_by_expression"))
        self.assertFalse(hasattr(fake, "camel_folder_search_free"))

    def test_missing_library_raises_oserror(self) -> None:
        with (
            _fresh_binding_state(),
            mock.patch(
                "post.mail.camel_util.ctypes.util.find_library", return_value=None
            ),
        ):
            with self.assertRaises(OSError):
                camel_util._get_libcamel("search")


class CtypesSearchSyncTests(unittest.TestCase):
    """The ctypes fallback used for non-UTF-8 UIDs."""

    def _run(self, sync_func):
        fake = _FakeCDLL({"camel_folder_search_sync": _Symbol(sync_func)})
        freed_arrays = mock.Mock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            mock.patch("post.mail.camel_util._get_libcamel", return_value=fake)
        )
        stack.enter_context(
            mock.patch(
                "post.mail.camel_util._gobject_pointer",
                return_value=ctypes.c_void_p(1),
            )
        )
        stack.enter_context(
            mock.patch("post.mail.camel_util._g_ptr_array_unref", freed_arrays)
        )
        stack.enter_context(mock.patch("post.mail.camel_util._g_error_free"))
        return freed_arrays, lambda: _folder_search_sync_via_ctypes(
            object(), "(match-all #t)", None
        )

    @staticmethod
    def _fill_array(out_ref, raw_uids: list[bytes]) -> list:
        buffers = [ctypes.create_string_buffer(raw) for raw in raw_uids]
        pointers = (ctypes.c_void_p * len(buffers))(
            *[ctypes.addressof(buffer) for buffer in buffers]
        )
        array = _GPtrArray(
            ctypes.cast(pointers, ctypes.POINTER(ctypes.c_void_p)), len(buffers)
        )
        out_ref._obj.contents = array
        return [buffers, pointers, array]

    def test_reads_uids_and_frees_the_array_with_g_ptr_array_unref(self) -> None:
        keep: list = []

        def sync(folder, expression, out_ref, cancel, error_ref):
            self.assertEqual(expression, b"(match-all #t)")
            keep.append(self._fill_array(out_ref, [b"12", b"\xa0\x90"]))
            return 1

        freed, call = self._run(sync)
        result = call()

        self.assertEqual(result, ["12", f"{_UID_B64_PREFIX}oJA="])
        freed.assert_called_once()

    def test_gerror_raises_search_error_and_frees_nothing_it_does_not_own(self) -> None:
        keep: list = []

        def sync(folder, expression, out_ref, cancel, error_ref):
            keep.append(ctypes.create_string_buffer(b"disk I/O error"))
            error_ref._obj.contents = _GError(
                0, 1, ctypes.cast(keep[-1], ctypes.c_char_p)
            )
            return 0

        freed, call = self._run(sync)

        with self.assertRaises(CamelSearchError) as ctx:
            call()

        self.assertIn("disk I/O error", str(ctx.exception))
        freed.assert_not_called()

    def test_false_return_without_error_raises(self) -> None:
        freed, call = self._run(lambda *args: 0)

        with self.assertRaises(CamelSearchError):
            call()

        freed.assert_not_called()

    def test_missing_symbol_raises_search_error_naming_the_floor(self) -> None:
        with mock.patch(
            "post.mail.camel_util._get_libcamel",
            side_effect=OSError("undefined symbol: camel_folder_search_sync"),
        ):
            with self.assertRaises(CamelSearchError) as ctx:
                _folder_search_sync_via_ctypes(object(), "(match-all #t)", None)

        self.assertIn("3.62", str(ctx.exception))


class FolderSearchIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("POST_EDS_TESTS"),
        "Set POST_EDS_TESTS=1 to run EDS integration tests",
    )
    def test_search_inbox_when_available(self) -> None:
        try:
            import gi

            gi.require_version("EDataServer", "1.2")
            gi.require_version("Camel", "1.2")
            from gi.repository import Camel, EDataServer

            from post.mail.eds import MailService
            from post.mail.search import MessageSearchQuery, SearchTerm
        except (ImportError, ValueError) as exc:
            self.skipTest(str(exc))

        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            self.skipTest("EDS registry unavailable")

        service = MailService.connect()
        accounts = service.list_accounts()
        if not accounts:
            self.skipTest("No mail accounts configured")

        account = accounts[0]
        folders = service.list_folders(account.uid)
        inbox = next(
            (
                folder["full_name"]
                for folder in folders
                if (folder.get("full_name") or "").upper() == "INBOX"
            ),
            folders[0]["full_name"] if folders else None,
        )
        if inbox is None:
            self.skipTest("No folders available")

        query = MessageSearchQuery(terms=(SearchTerm(field="text", value="the"),))
        messages, match_count, total, source = service.search_folder_messages(
            account.uid,
            inbox,
            query,
            sync=False,
        )
        self.assertIsInstance(messages, list)
        self.assertGreaterEqual(match_count, 0)
        self.assertGreaterEqual(total, 0)
        self.assertIn(source, ("memory", "disk_cache", "local", "server"))

        messages2, match_count2, _total2, _source2 = service.search_folder_messages(
            account.uid,
            inbox,
            query,
            sync=False,
        )
        self.assertIsInstance(messages2, list)
        self.assertEqual(match_count2, match_count)


if __name__ == "__main__":
    unittest.main()
