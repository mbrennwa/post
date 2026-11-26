# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""The correspondent cache file is the source of truth (#547).

The camel helper and the UI both write ``correspondents.json``. The UI must
pick up the helper's writes (stale index, file appearing after the composer
opened) and must never overwrite them with a stale in-memory copy.
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("WebKit", "6.0")

from post.compose_window import _CORRESPONDENTS_NOT_LOADED, ComposeWindow
from post.mail import correspondent_cache, folder_index_cache
from post.mail.correspondents import Correspondent, aggregate_correspondents
from post.mail.eds import MailService, _FolderMessageIndex


def _person(email: str) -> Correspondent:
    return Correspondent(
        display=email, email=email, name="", last_seen=1, received_count=1
    )


def _helper_writes(folder: str, *emails: str) -> None:
    """What the helper process does: update one folder in the cache file."""
    correspondent_cache.update_folder(
        "acct-1", folder, [_person(email) for email in emails], aggregate_correspondents
    )


def _index(*senders: str) -> _FolderMessageIndex:
    return _FolderMessageIndex(
        messages=[
            {"uid": str(n), "from": sender, "sort_date": 10 + n}
            for n, sender in enumerate(senders, start=1)
        ],
        unread=0,
        total=len(senders),
    )


class _CacheRootTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._patches = [
            mock.patch.object(
                folder_index_cache, "_CACHE_ROOT", root / "folder-index"
            ),
            mock.patch.object(
                correspondent_cache, "_CACHE_ROOT", root / "correspondents"
            ),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmpdir.cleanup()


class MailServiceCacheRefreshTests(_CacheRootTestCase):
    def _service(self) -> MailService:
        service = MailService(registry=mock.Mock())
        service._build_folder_index_unlocked = mock.Mock(
            side_effect=AssertionError("must not open folders for correspondents")
        )
        service._get_store_unlocked = mock.Mock(
            side_effect=AssertionError("must not connect for correspondents")
        )
        return service

    @staticmethod
    def _emails(items: list[Correspondent]) -> set[str]:
        return {item.email for item in items}

    def test_file_appearing_after_empty_load_is_picked_up(self) -> None:
        service = self._service()
        self.assertEqual(service._get_correspondents_unlocked("acct-1"), [])
        self.assertNotIn("acct-1", service._correspondent_indexes)

        _helper_writes("INBOX", "alice@example.com")

        self.assertEqual(
            self._emails(service._get_correspondents_unlocked("acct-1")),
            {"alice@example.com"},
        )

    def test_index_follows_helper_writes(self) -> None:
        _helper_writes("INBOX", "alice@example.com")
        service = self._service()
        self.assertEqual(
            self._emails(service._get_correspondents_unlocked("acct-1")),
            {"alice@example.com"},
        )

        _helper_writes("Sent", "bob@example.com")

        self.assertEqual(
            self._emails(service._get_correspondents_unlocked("acct-1")),
            {"alice@example.com", "bob@example.com"},
        )

    def test_unchanged_file_is_not_parsed_again(self) -> None:
        _helper_writes("INBOX", "alice@example.com")
        service = self._service()
        service._get_correspondents_unlocked("acct-1")

        with mock.patch.object(
            correspondent_cache,
            "load_snapshot",
            side_effect=AssertionError("unchanged file must not be re-read"),
        ):
            result = service._get_correspondents_unlocked("acct-1")
        self.assertEqual(self._emails(result), {"alice@example.com"})

    def test_own_write_is_not_reread(self) -> None:
        service = self._service()
        service._store_folder_index("acct-1", "INBOX", _index("Al <al@example.com>"))

        with mock.patch.object(
            correspondent_cache,
            "load_snapshot",
            side_effect=AssertionError("own write must not be re-read"),
        ):
            result = service._get_correspondents_unlocked("acct-1")
        self.assertEqual(self._emails(result), {"al@example.com"})

    def test_ui_merge_keeps_what_the_helper_wrote(self) -> None:
        """A UI-side folder store must not overwrite the helper's folders."""
        service = self._service()
        service._store_folder_index("acct-1", "INBOX", _index("Al <al@example.com>"))
        # The UI's in-memory copy is now stale: the helper adds a folder.
        _helper_writes("Sent", "helper@example.com")

        service._store_folder_index(
            "acct-1", "INBOX", _index("Al <al@example.com>", "Cy <cy@example.com>")
        )

        folders = correspondent_cache.load_folders("acct-1")
        self.assertEqual(set(folders), {"INBOX", "Sent"})
        self.assertEqual(
            self._emails(correspondent_cache.load("acct-1")),
            {"al@example.com", "cy@example.com", "helper@example.com"},
        )
        self.assertEqual(
            self._emails(service.get_correspondents_cached("acct-1")),
            {"al@example.com", "cy@example.com", "helper@example.com"},
        )

    def test_ui_merge_reuses_memory_when_file_is_unchanged(self) -> None:
        service = self._service()
        service._store_folder_index("acct-1", "INBOX", _index("Al <al@example.com>"))

        with mock.patch.object(
            correspondent_cache,
            "_read_payload",
            side_effect=AssertionError("unchanged file must not be parsed"),
        ):
            service._store_folder_index(
                "acct-1", "Archive", _index("Bo <bo@example.com>")
            )

        self.assertEqual(
            set(correspondent_cache.load_folders("acct-1")), {"INBOX", "Archive"}
        )

    def test_invalidate_drops_memory_and_stamp(self) -> None:
        service = self._service()
        service._store_folder_index("acct-1", "INBOX", _index("Al <al@example.com>"))

        service.invalidate_correspondent_index("acct-1")

        self.assertNotIn("acct-1", service._correspondent_indexes)
        self.assertNotIn("acct-1", service._correspondent_stamps)
        self.assertNotIn("acct-1", service._correspondent_folder_stats)
        self.assertIsNone(correspondent_cache.stamp("acct-1"))

    def test_correspondents_stamp_follows_cache_file(self) -> None:
        service = self._service()
        self.assertIsNone(service.correspondents_stamp("acct-1"))
        _helper_writes("INBOX", "alice@example.com")
        first = service.correspondents_stamp("acct-1")
        self.assertIsNotNone(first)
        _helper_writes("Sent", "bob@example.com")
        self.assertNotEqual(service.correspondents_stamp("acct-1"), first)


class ComposerReloadTests(unittest.TestCase):
    """The composer reloads only when the cache file's stamp moved."""

    def _stub(self, stamp, loaded_at) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            _mail=mock.Mock(correspondents_stamp=mock.Mock(return_value=stamp)),
            _correspondents_stamp=loaded_at,
            _load_correspondents=mock.Mock(),
        )

    def test_reloads_when_file_appeared_after_open(self) -> None:
        stub = self._stub(stamp=(1, 2, 3), loaded_at=None)  # None: no file at load
        ComposeWindow._reload_correspondents_if_cache_changed(stub, "acct-1")
        stub._load_correspondents.assert_called_once_with()

    def test_reloads_when_file_changed(self) -> None:
        stub = self._stub(stamp=(2, 2, 3), loaded_at=(1, 2, 3))
        ComposeWindow._reload_correspondents_if_cache_changed(stub, "acct-1")
        stub._load_correspondents.assert_called_once_with()

    def test_no_reload_when_file_unchanged(self) -> None:
        stub = self._stub(stamp=(1, 2, 3), loaded_at=(1, 2, 3))
        ComposeWindow._reload_correspondents_if_cache_changed(stub, "acct-1")
        stub._load_correspondents.assert_not_called()

    def test_no_reload_loop_while_file_is_still_missing(self) -> None:
        # The load that found no file recorded stamp None: asking again with
        # the file still missing must not start another load.
        stub = self._stub(stamp=None, loaded_at=None)
        ComposeWindow._reload_correspondents_if_cache_changed(stub, "acct-1")
        stub._load_correspondents.assert_not_called()

    def test_reloads_when_nothing_loaded_yet(self) -> None:
        stub = self._stub(stamp=None, loaded_at=_CORRESPONDENTS_NOT_LOADED)
        ComposeWindow._reload_correspondents_if_cache_changed(stub, "acct-1")
        stub._load_correspondents.assert_called_once_with()

    def _loading_stub(self, stamp) -> types.SimpleNamespace:
        submitted: list = []
        mail = mock.Mock()
        mail.correspondents_stamp.return_value = stamp
        mail.submit_interactive.side_effect = lambda _name, worker: submitted.append(
            worker
        )
        stub = types.SimpleNamespace(
            _mail=mail,
            _correspondents_generation=0,
            _correspondents_stamp=_CORRESPONDENTS_NOT_LOADED,
            _selected_account=lambda: types.SimpleNamespace(uid="acct-1"),
            _on_correspondents_loaded=mock.Mock(),
            _on_correspondents_load_failed=mock.Mock(),
        )
        stub.submitted = submitted
        return stub

    def test_load_records_stamp_taken_before_the_load(self) -> None:
        stub = self._loading_stub(stamp=(1, 2, 3))
        ComposeWindow._load_correspondents(stub)
        self.assertEqual(stub._correspondents_stamp, (1, 2, 3))
        self.assertEqual(stub._correspondents_generation, 1)
        self.assertEqual(len(stub.submitted), 1)

    def test_failed_load_is_reported_so_it_can_be_retried(self) -> None:
        stub = self._loading_stub(stamp=(1, 2, 3))
        stub._mail.get_correspondents.side_effect = RuntimeError("boom")
        ComposeWindow._load_correspondents(stub)
        with (
            mock.patch("post.compose_window.GLib.idle_add") as idle_add,
            self.assertLogs("post.compose_window", "WARNING"),
        ):
            stub.submitted[0]()
        idle_add.assert_called_once_with(stub._on_correspondents_load_failed, 1)

    def test_load_failure_forgets_stamp_only_for_current_generation(self) -> None:
        stub = types.SimpleNamespace(
            _correspondents_generation=2, _correspondents_stamp=(1, 2, 3)
        )
        ComposeWindow._on_correspondents_load_failed(stub, 1)  # superseded
        self.assertEqual(stub._correspondents_stamp, (1, 2, 3))
        ComposeWindow._on_correspondents_load_failed(stub, 2)
        self.assertIs(stub._correspondents_stamp, _CORRESPONDENTS_NOT_LOADED)


if __name__ == "__main__":
    unittest.main()
