# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Contract tests against the installed Camel / Evolution Data Server (#535).

Most Camel tests use fakes, and a fake accepts any API, so they pass even when
the real library dropped a function (EDS 3.62 removed
``camel_folder_search_by_expression`` and every search failed while the tests
stayed green). The tests here use the real library and nothing else:

* every Camel call Post makes still exists on the installed EDS,
* the hard-coded ``CamelFolderInfo`` struct still matches the typelib,
* the removed search call is really gone, so no code path can rely on it.

They skip only when PyGObject or the Camel typelib is not installed at all.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import re
import unittest
from pathlib import Path
from unittest import mock

try:
    import gi

    gi.require_version("Camel", "1.2")
    gi.require_version("EDataServer", "1.2")
    from gi.repository import Camel, EDataServer

    _HAVE_CAMEL = True
except (ImportError, ValueError):  # pragma: no cover - environment dependent
    _HAVE_CAMEL = False

from post.mail import camel_compat, camel_util

SRC = Path(__file__).resolve().parents[1] / "src" / "post"


@unittest.skipUnless(_HAVE_CAMEL, "Camel / EDataServer typelibs not installed")
class InstalledCamelContractTests(unittest.TestCase):
    def test_eds_meets_the_minimum_version(self) -> None:
        self.assertIsNone(
            EDataServer.eds_check_version(*camel_compat.MIN_EDS_VERSION),
            f"Post needs EDS {camel_compat.MIN_EDS_TEXT} or newer",
        )

    def test_compat_check_reports_no_problems(self) -> None:
        self.assertEqual(camel_compat.check_camel_compat(), [])

    def test_every_required_gi_method_exists(self) -> None:
        self.assertEqual(camel_compat._missing_gi_methods(Camel), [])

    def test_search_sync_has_the_signature_post_calls(self) -> None:
        # search_sync(expression, cancellable) -> (ok, uids)
        info = Camel.Folder.search_sync
        self.assertTrue(callable(info))
        self.assertIn("search_sync", repr(info))
        self.assertIn("out_uids", info.__doc__ or "")
        self.assertIn("cancellable", info.__doc__ or "")

    def test_required_libcamel_symbols_resolve(self) -> None:
        self.assertEqual(camel_compat._missing_camel_symbols(), [])

    def test_ctypes_bindings_bind_on_the_real_library(self) -> None:
        with mock.patch.multiple(
            "post.mail.camel_util",
            _libcamel_lib=None,
            _libcamel_bound=set(),
            _libcamel_folder_uids=None,
        ):
            for group in camel_util._LIBCAMEL_GROUPS:
                lib = camel_util._get_libcamel(group)
            self.assertIsInstance(lib, ctypes.CDLL)
            self.assertEqual(lib.camel_folder_search_sync.restype, ctypes.c_int)

    def test_search_sync_is_exported_by_libcamel(self) -> None:
        # The removed pair (REMOVED_IN_3_62) is deliberately not asserted
        # absent: Post must not need it, but its presence would be harmless.
        path = ctypes.util.find_library("camel-1.2")
        self.assertTrue(path, "libcamel-1.2 not found")
        lib = ctypes.CDLL(path)
        self.assertTrue(hasattr(lib, "camel_folder_search_sync"))

    def test_folder_info_struct_matches_the_typelib(self) -> None:
        self.assertEqual(camel_compat.folder_info_layout_problems(Camel), [])

    def test_folder_info_child_and_next_are_still_raw_pointers(self) -> None:
        # helpers.py walks the tree through ctypes because GI hands child/next
        # out as integers. If GI starts returning objects, the struct code can
        # be replaced (and this test updated).
        info = Camel.FolderInfo.__info__
        names = {field.get_name() for field in info.get_fields()}
        self.assertTrue({"child", "next", "full_name", "flags"} <= names)
        folder_info = Camel.FolderInfo()
        self.assertIsInstance(folder_info.child, int)
        self.assertIsInstance(folder_info.next, int)


class SourceUsesOnlyExistingSearchApiTests(unittest.TestCase):
    """Static guard: no module may bind the removed search symbols again."""

    def test_removed_search_functions_are_not_referenced(self) -> None:
        pattern = re.compile(r"camel_folder_search_(by_expression|free)\b")
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "camel_compat.py":
                continue  # lists them on purpose (REMOVED_IN_3_62)
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


class FakeCamel:
    """Typelib stand-in with chosen classes and methods."""

    def __init__(self, classes: dict[str, tuple[str, ...]]):
        for class_name, methods in classes.items():
            setattr(
                self,
                class_name,
                type(class_name, (), {name: (lambda self: None) for name in methods}),
            )


class CompatCheckWithFakesTests(unittest.TestCase):
    def _complete_fake(self) -> FakeCamel:
        return FakeCamel(dict(camel_compat.REQUIRED_GI_METHODS))

    def test_complete_api_reports_nothing_missing(self) -> None:
        self.assertEqual(camel_compat._missing_gi_methods(self._complete_fake()), [])

    def test_missing_method_is_named(self) -> None:
        classes = {k: tuple(v) for k, v in camel_compat.REQUIRED_GI_METHODS.items()}
        classes["Folder"] = tuple(m for m in classes["Folder"] if m != "search_sync")

        missing = camel_compat._missing_gi_methods(FakeCamel(classes))

        self.assertEqual(missing, ["Camel.Folder.search_sync"])

    def test_missing_class_is_named(self) -> None:
        classes = {k: tuple(v) for k, v in camel_compat.REQUIRED_GI_METHODS.items()}
        del classes["OfflineStore"]

        missing = camel_compat._missing_gi_methods(FakeCamel(classes))

        self.assertEqual(missing, ["Camel.OfflineStore"])

    def test_old_eds_is_reported_with_the_found_version(self) -> None:
        class FakeEds:
            EDS_MAJOR_VERSION = 3
            EDS_MINOR_VERSION = 56
            EDS_MICRO_VERSION = 2

            @staticmethod
            def eds_check_version(major, minor, micro):
                return "EDS version too old (minor mismatch)"

        with (
            mock.patch.object(
                camel_compat, "_load_gi", return_value=(self._complete_fake(), FakeEds)
            ),
            mock.patch.object(camel_compat, "_missing_camel_symbols", return_value=[]),
            mock.patch.object(
                camel_compat, "folder_info_layout_problems", return_value=[]
            ),
        ):
            problems = camel_compat.check_camel_compat()

        self.assertEqual(len(problems), 1)
        self.assertIn("3.62 or newer is required", problems[0])
        self.assertIn("found 3.56.2", problems[0])

    def test_missing_typelib_gives_an_install_hint(self) -> None:
        with mock.patch.object(
            camel_compat, "_load_gi", side_effect=ValueError("Namespace Camel not available")
        ):
            problems = camel_compat.check_camel_compat()

        self.assertEqual(len(problems), 1)
        self.assertIn("gir1.2-camel-1.2", problems[0])

    def test_struct_layout_mismatch_is_reported(self) -> None:
        class FakeField:
            def __init__(self, name, offset):
                self._name, self._offset = name, offset

            def get_name(self):
                return self._name

            def get_offset(self):
                return self._offset

        class FakeInfo:
            @staticmethod
            def get_size():
                return 64

            @staticmethod
            def get_fields():
                # A new field was inserted ahead of ``full_name``.
                return [
                    FakeField("next", 0),
                    FakeField("parent", 8),
                    FakeField("child", 16),
                    FakeField("full_name", 32),
                    FakeField("display_name", 40),
                    FakeField("flags", 48),
                    FakeField("unread", 52),
                    FakeField("total", 56),
                ]

        class FakeFolderInfo:
            __info__ = FakeInfo

        class FakeCamelModule:
            FolderInfo = FakeFolderInfo

        problems = camel_compat.folder_info_layout_problems(FakeCamelModule)

        self.assertTrue(any("64 bytes" in p for p in problems), problems)
        self.assertTrue(any("full_name" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
