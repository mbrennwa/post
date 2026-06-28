# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel folder search integration."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class FolderSearchUidTests(unittest.TestCase):
    def test_empty_scope_skips_search(self) -> None:
        folder = mock.Mock()

        with mock.patch("post.mail.eds.folder_search_uids") as folder_search_uids:
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "missing"))',
                [],
            )

        self.assertEqual(result, set())
        folder_search_uids.assert_not_called()

    def test_non_empty_matches_return_uid_set(self) -> None:
        folder = mock.Mock()
        index_uids = ["uid-1", "uid-2", "uid-3"]

        with mock.patch(
            "post.mail.eds.folder_search_uids",
            return_value=["uid-1", "uid-2"],
        ) as folder_search_uids:
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                index_uids,
            )

        self.assertEqual(result, {"uid-1", "uid-2"})
        folder_search_uids.assert_called_once_with(
            folder,
            '(match-all (header-contains "Subject" "invoice"))',
            index_uids,
        )


if __name__ == "__main__":
    unittest.main()
