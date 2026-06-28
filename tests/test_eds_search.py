# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for Camel folder search integration."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class FolderSearchUidTests(unittest.TestCase):
    def test_empty_matches_skip_free_result(self) -> None:
        folder = mock.Mock()
        folder_search = mock.Mock()
        folder_search.search.return_value = []

        with mock.patch(
            "post.mail.eds.Camel.FolderSearch.new", return_value=folder_search
        ):
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "missing"))',
                only_cached=False,
            )

        self.assertEqual(result, set())
        folder_search.free_result.assert_not_called()

    def test_non_empty_matches_return_uid_set(self) -> None:
        folder = mock.Mock()
        folder_search = mock.Mock()
        folder_search.search.return_value = ["uid-1", "uid-2"]

        with mock.patch(
            "post.mail.eds.Camel.FolderSearch.new", return_value=folder_search
        ):
            service = MailService(registry=mock.Mock())
            result = service._folder_search_uids_unlocked(
                folder,
                '(match-all (header-contains "Subject" "invoice"))',
                only_cached=True,
            )

        self.assertEqual(result, {"uid-1", "uid-2"})
        folder_search.set_only_cached_messages.assert_called_once_with(True)
        folder_search.free_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
