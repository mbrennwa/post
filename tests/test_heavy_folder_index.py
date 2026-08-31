# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for heavy-folder index grow-only cache helpers (#208)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import (
    _heavy_folder_camel_behind_server,
    _should_save_heavy_folder_index,
)


class HeavyFolderIndexCacheTests(unittest.TestCase):
    def test_save_when_no_existing(self) -> None:
        self.assertTrue(
            _should_save_heavy_folder_index([{"uid": "1"}], None)
        )
        self.assertFalse(_should_save_heavy_folder_index([], None))

    def test_save_only_when_growing(self) -> None:
        existing = ([{"uid": "1"}, {"uid": "2"}], 0, 100)
        self.assertFalse(
            _should_save_heavy_folder_index([{"uid": "1"}], existing)
        )
        self.assertFalse(
            _should_save_heavy_folder_index(
                [{"uid": "1"}, {"uid": "2"}], existing
            )
        )
        self.assertTrue(
            _should_save_heavy_folder_index(
                [{"uid": "1"}, {"uid": "2"}, {"uid": "3"}], existing
            )
        )


class HeavyFolderIncompleteDeltaTests(unittest.TestCase):
    def test_camel_behind_server_gap(self) -> None:
        self.assertFalse(_heavy_folder_camel_behind_server(5900, 0))
        self.assertFalse(_heavy_folder_camel_behind_server(5900, 5950))
        self.assertTrue(_heavy_folder_camel_behind_server(5900, 28268))
        self.assertTrue(_heavy_folder_camel_behind_server(0, 28268))

    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.folder_search_all_uids", return_value=[])
    @mock.patch("post.mail.eds.folder_status_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_empty_refresh_while_behind_forces_prepare(
        self,
        folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        status_cache: mock.Mock,
        _search_all: mock.Mock,
        index_cache: mock.Mock,
    ) -> None:
        """Finished refresh with no new UIDs but Camel << STATUS keeps alive."""
        from post.mail.eds import MailService, _FolderMessageIndex

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        io_thread.pump_until.return_value = True
        get_io.return_value = io_thread
        status_cache.load.return_value = (100, 28268)
        status_cache.index_caught_up.return_value = False
        index_cache.load.return_value = None
        index_cache.save = mock.Mock()

        messages = [
            {"uid": str(i), "subject": f"m{i}"} for i in range(5900)
        ]
        folder_get_uids.return_value = [str(i) for i in range(5900)]

        folder = mock.Mock()
        folder.prepare_content_refresh = mock.Mock()
        folder.refresh_info = mock.Mock()
        folder.refresh_info_finish = mock.Mock()
        folder.get_message_count.return_value = 5900

        mail = MailService.__new__(MailService)
        mail._lock = mock.MagicMock()
        mail._accounts_by_uid = {"acct": mock.Mock(backend="imapx")}
        mail._heavy_index_sessions = {}
        mail._folder_indexes = {
            ("acct", "Archive"): _FolderMessageIndex(
                messages=messages, unread=0, total=28268
            )
        }
        mail._open_folder_unlocked = mock.Mock(return_value=folder)
        mail._cached_folder_stats_unlocked = mock.Mock(
            return_value=(100, 28268)
        )
        mail._register_heavy_index_refresh_cancellable = mock.Mock()
        mail._unregister_heavy_index_refresh_cancellable = mock.Mock()

        def _refresh(_prio, _canc, callback, _data):
            callback(folder, mock.Mock(), None)

        folder.refresh_info.side_effect = _refresh

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            cursor={
                "pending_server_refresh": True,
                "status_seeded": True,
                "did_prepare_content_refresh": True,
            },
            allow_refresh=True,
        )
        self.assertFalse(progress.done)
        self.assertTrue(progress.cursor.get("force_prepare_incomplete_delta"))
        self.assertFalse(progress.cursor.get("did_prepare_content_refresh"))
        self.assertTrue(
            mail._heavy_index_sessions[("acct", "Archive")].get(
                "force_prepare_incomplete_delta"
            )
        )
        # First pass skipped prepare (large index); keep-alive arms force flag.
        folder.prepare_content_refresh.assert_not_called()

        # Next slice must honor force_prepare even with a large index.
        progress2 = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            cursor=progress.cursor,
            allow_refresh=True,
        )
        folder.prepare_content_refresh.assert_called_once()
        self.assertFalse(progress2.done)
        # Still behind after prepare+empty refresh → keep-alive arms again.
        self.assertTrue(progress2.cursor.get("force_prepare_incomplete_delta"))


class HeavyFolderIndexInteractiveYieldTests(unittest.TestCase):
    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_interactive_pending_does_not_set_refresh_done(
        self,
        _folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        index_cache: mock.Mock,
    ) -> None:
        """Interactive work must yield without permanently skipping refresh."""
        from post.mail.eds import MailService, _FolderMessageIndex

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = True
        get_io.return_value = io_thread
        index_cache.load.return_value = None

        mail = MailService.__new__(MailService)
        mail._lock = mock.MagicMock()
        mail._accounts_by_uid = {"acct": mock.Mock(backend="imapx")}
        mail._heavy_index_sessions = {}
        mail._folder_indexes = {
            ("acct", "Archive"): _FolderMessageIndex(
                messages=[{"uid": "1"}], unread=0, total=28177
            )
        }
        mail._open_folder_unlocked = mock.Mock(return_value=mock.Mock())
        mail._cached_folder_stats_unlocked = mock.Mock(
            return_value=(3803, 28177)
        )

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            cursor={"pending_server_refresh": True},
            allow_refresh=True,
        )
        self.assertFalse(progress.done)
        self.assertFalse(progress.cursor.get("refresh_done"))
        self.assertTrue(progress.cursor.get("yield_for_interactive"))
        self.assertTrue(progress.cursor.get("pending_server_refresh"))


class HeavyFolderPrepareSkipTests(unittest.TestCase):
    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.folder_search_all_uids", return_value=[])
    @mock.patch("post.mail.eds.folder_status_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_skips_prepare_when_index_already_large(
        self,
        folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        status_cache: mock.Mock,
        _search_all: mock.Mock,
        index_cache: mock.Mock,
    ) -> None:
        """Large local indexes must not call prepare on the first refresh (#208)."""
        from post.mail.eds import (
            MailService,
            _FolderMessageIndex,
            _HEAVY_FOLDER_PREPARE_MIN_INDEXED,
        )

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        io_thread.pump_until.return_value = True
        get_io.return_value = io_thread
        # Caught-up STATUS so empty refresh is a normal stall, not incomplete
        # keep-alive (that path is covered separately).
        status_total = _HEAVY_FOLDER_PREPARE_MIN_INDEXED
        status_cache.load.return_value = (0, status_total)
        status_cache.index_caught_up.return_value = True
        index_cache.load.return_value = None
        index_cache.save = mock.Mock()

        messages = [
            {"uid": str(i), "subject": f"m{i}"}
            for i in range(_HEAVY_FOLDER_PREPARE_MIN_INDEXED)
        ]
        folder_get_uids.return_value = [str(i) for i in range(len(messages))]

        folder = mock.Mock()
        folder.prepare_content_refresh = mock.Mock()
        folder.refresh_info = mock.Mock()
        folder.refresh_info_finish = mock.Mock()
        folder.get_message_count.return_value = len(messages)

        mail = MailService.__new__(MailService)
        mail._lock = mock.MagicMock()
        mail._accounts_by_uid = {"acct": mock.Mock(backend="imapx")}
        mail._heavy_index_sessions = {}
        mail._folder_indexes = {
            ("acct", "Archive"): _FolderMessageIndex(
                messages=messages, unread=0, total=status_total
            )
        }
        mail._open_folder_unlocked = mock.Mock(return_value=folder)
        mail._cached_folder_stats_unlocked = mock.Mock(
            return_value=(0, status_total)
        )
        mail._register_heavy_index_refresh_cancellable = mock.Mock()
        mail._unregister_heavy_index_refresh_cancellable = mock.Mock()

        def _refresh(_prio, _canc, callback, _data):
            callback(folder, mock.Mock(), None)

        folder.refresh_info.side_effect = _refresh

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            cursor={
                "pending_server_refresh": True,
                "status_seeded": True,
            },
            allow_refresh=True,
        )
        folder.prepare_content_refresh.assert_not_called()
        self.assertFalse(
            progress.cursor.get("force_prepare_incomplete_delta")
        )
        self.assertTrue(
            mail._heavy_index_sessions[("acct", "Archive")].get(
                "did_prepare_content_refresh"
            )
        )


class OfflineSyncNoHeavyIndexTests(unittest.TestCase):
    @mock.patch("post.mail.offline_sync.get_mail_io_thread")
    def test_run_account_sync_does_not_call_heavy_folder_index(
        self, get_io: mock.Mock,
    ) -> None:
        """Offline body sync may re-index local summary only (no refresh_info)."""
        from post.mail.offline_sync import OfflineBodySyncCoordinator

        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread

        mail = mock.Mock()
        mail.get_account.return_value = mock.Mock(display_label="Test")
        mail.continue_heavy_folder_index = mock.Mock()
        mail.offline_body_sync_is_held.return_value = False
        coordinator = OfflineBodySyncCoordinator(mail)

        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        offline_folder = mock.Mock(spec=Camel.OfflineFolder)
        offline_folder.get_full_name.return_value = "Archive"
        offline_folder.can_downsync.return_value = True
        cancellable = mock.Mock()
        cancellable.is_cancelled.return_value = False

        with mock.patch(
            "post.mail.offline_sync.apply_offline_sync_to_folder"
        ):
            with mock.patch.object(coordinator, "_downsync_folder_sync") as downsync:
                complete = coordinator._run_account_sync(
                    "acct-1",
                    "all",
                    cancellable,
                    folders=[offline_folder],
                    folder_index=0,
                )

        self.assertTrue(complete)
        downsync.assert_called_once()
        mail.continue_heavy_folder_index.assert_called_once_with(
            "acct-1", "Archive", allow_refresh=False
        )


class HeavyFolderInvalidateTests(unittest.TestCase):
    @mock.patch("post.mail.eds.folder_index_cache")
    def test_invalidate_skips_heavy_folders(self, cache: mock.Mock) -> None:
        """Folder::changed must not delete progressive Archive indexes (#208)."""
        from post.mail.eds import MailService, _FolderMessageIndex

        mail = MailService.__new__(MailService)
        mail._folder_indexes = {
            ("acct", "Archive"): _FolderMessageIndex(
                messages=[{"uid": str(i)} for i in range(2000)],
                unread=0,
                total=28000,
            ),
            ("acct", "INBOX"): _FolderMessageIndex(
                messages=[{"uid": "1"}], unread=0, total=1
            ),
        }
        cache.load.return_value = (
            [{"uid": str(i)} for i in range(2000)],
            0,
            28000,
        )

        mail._invalidate_folder_index("acct", "Archive")
        self.assertIn(("acct", "Archive"), mail._folder_indexes)
        cache.invalidate.assert_not_called()

        mail._invalidate_folder_index("acct", "INBOX")
        self.assertNotIn(("acct", "INBOX"), mail._folder_indexes)
        cache.invalidate.assert_called_once_with("acct", "INBOX")


def _heavy_mail_stub(
    *,
    backend: str = "imapx",
    messages: list[dict] | None = None,
    unread: int = 0,
    total: int = 0,
    folder: object | None = None,
):
    from post.mail.eds import MailService, _FolderMessageIndex

    mail = MailService.__new__(MailService)
    mail._lock = mock.MagicMock()
    mail._accounts_by_uid = {"acct": mock.Mock(backend=backend)}
    mail._heavy_index_sessions = {}
    mail._correspondent_indexes = {}
    rows = messages if messages is not None else [{"uid": "1"}]
    mail._folder_indexes = {
        ("acct", "Archive"): _FolderMessageIndex(
            messages=rows, unread=unread, total=total
        )
    }
    mail._open_folder_unlocked = mock.Mock(
        return_value=folder if folder is not None else mock.Mock()
    )
    return mail


class HeavyFolderListUnionDiskTests(unittest.TestCase):
    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_offline_slice_keeps_disk_only_rows(
        self,
        folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        cache: mock.Mock,
    ) -> None:
        """RAM Camel window must union grow-only disk before local-only index (#365)."""
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread
        folder_get_uids.return_value = ["1"]
        ram = [{"uid": "1", "subject": "new", "sort_date": 200}]
        disk = [
            {"uid": "1", "subject": "new", "sort_date": 200},
            {"uid": "2", "subject": "old", "sort_date": 100},
        ]
        cache.load.return_value = (disk, 0, 2)
        cache.save = mock.Mock()

        mail = _heavy_mail_stub(messages=ram, total=1)
        mail._cached_folder_stats_unlocked = mock.Mock(return_value=(0, 2))

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            allow_refresh=False,
        )
        uids = {message["uid"] for message in progress.messages}
        self.assertEqual(uids, {"1", "2"})
        self.assertTrue(progress.done)

    @mock.patch("post.mail.eds.folder_index_cache")
    def test_heavy_list_index_unions_disk_into_ram(
        self, cache: mock.Mock
    ) -> None:
        ram = [{"uid": "1", "subject": "new", "sort_date": 200}]
        disk = [
            {"uid": "1", "subject": "new", "sort_date": 200},
            {"uid": "2", "subject": "old", "sort_date": 100},
        ]
        cache.load.return_value = (disk, 1, 2)
        mail = _heavy_mail_stub(messages=ram, unread=0, total=1)
        index, source = mail._get_folder_index_unlocked(
            "acct", "Archive", sync=True
        )
        self.assertEqual({message["uid"] for message in index.messages}, {"1", "2"})
        self.assertEqual(index.total, 2)
        self.assertEqual(source, "disk_cache")
        stored = mail._folder_indexes[("acct", "Archive")]
        self.assertEqual({message["uid"] for message in stored.messages}, {"1", "2"})


class HeavyFolderImapExtraUidTests(unittest.TestCase):
    @mock.patch("post.mail.eds.message_info_to_dict")
    @mock.patch("post.mail.eds.folder_get_message_info")
    @mock.patch("post.mail.eds.folder_search_all_uids")
    @mock.patch("post.mail.eds.folder_status_cache")
    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_imap_search_queues_uids_missing_from_camel(
        self,
        folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        cache: mock.Mock,
        status_cache: mock.Mock,
        search_all: mock.Mock,
        get_info: mock.Mock,
        info_to_dict: mock.Mock,
    ) -> None:
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        get_io.return_value = io_thread
        folder_get_uids.return_value = ["1", "2"]
        rows = [
            {"uid": "1", "subject": "a", "sort_date": 2},
            {"uid": "2", "subject": "b", "sort_date": 1},
        ]
        cache.load.return_value = (rows, 0, 2)
        cache.save = mock.Mock()
        status_cache.load.return_value = (0, 1000)
        status_cache.index_caught_up.return_value = False
        search_all.return_value = ["1", "2", "99"]
        get_info.return_value = mock.Mock()
        info_to_dict.side_effect = lambda _info, uid=None, backend=None: {
            "uid": uid,
            "subject": "extra",
            "sort_date": 3,
            "flags": {"seen": True},
        }

        mail = _heavy_mail_stub(messages=rows, total=1000)
        mail._cached_folder_stats_unlocked = mock.Mock(return_value=(0, 1000))
        mail._register_heavy_index_refresh_cancellable = mock.Mock()
        mail._unregister_heavy_index_refresh_cancellable = mock.Mock()

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            allow_refresh=True,
        )
        search_all.assert_called_once()
        self.assertEqual(search_all.call_args.args[1], "(match-all #t)")
        uids = {message["uid"] for message in progress.messages}
        self.assertIn("99", uids)
        self.assertTrue(
            mail._heavy_index_sessions[("acct", "Archive")].get(
                "did_server_uid_search"
            )
        )

    @mock.patch("post.mail.eds.folder_search_all_uids")
    @mock.patch("post.mail.eds.folder_status_cache")
    @mock.patch("post.mail.eds.folder_index_cache")
    @mock.patch("post.mail.eds.get_mail_io_thread")
    @mock.patch("post.mail.eds.folder_get_uids")
    def test_microsoft365_skips_imap_uid_search(
        self,
        folder_get_uids: mock.Mock,
        get_io: mock.Mock,
        cache: mock.Mock,
        status_cache: mock.Mock,
        search_all: mock.Mock,
    ) -> None:
        io_thread = mock.Mock()
        io_thread.has_interactive_work_pending.return_value = False
        io_thread.pump_until.return_value = True
        get_io.return_value = io_thread
        folder_get_uids.return_value = ["1"]
        rows = [{"uid": "1", "subject": "a", "sort_date": 1}]
        cache.load.return_value = (rows, 0, 1)
        cache.save = mock.Mock()
        status_cache.load.return_value = (0, 1000)
        status_cache.index_caught_up.return_value = False
        status_cache.scrub_if_summary_echo = mock.Mock()

        folder = mock.Mock()
        folder.prepare_content_refresh = mock.Mock()
        folder.refresh_info = mock.Mock()
        folder.refresh_info_finish = mock.Mock()
        folder.get_message_count.return_value = 1

        def _refresh(_prio, _canc, callback, _data):
            callback(folder, mock.Mock(), None)

        folder.refresh_info.side_effect = _refresh

        mail = _heavy_mail_stub(
            backend="microsoft365", messages=rows, total=1000, folder=folder
        )
        mail._cached_folder_stats_unlocked = mock.Mock(return_value=(0, 1000))
        mail._register_heavy_index_refresh_cancellable = mock.Mock()
        mail._unregister_heavy_index_refresh_cancellable = mock.Mock()
        mail._seed_heavy_folder_status_from_graph_unlocked = mock.Mock()

        progress = mail._continue_heavy_folder_index_unlocked(
            "acct",
            "Archive",
            allow_refresh=True,
        )
        search_all.assert_not_called()
        self.assertTrue(progress.cursor.get("pending_server_refresh"))


if __name__ == "__main__":
    unittest.main()
