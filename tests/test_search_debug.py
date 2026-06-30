# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import unittest
from unittest import mock

from post.mail import search_debug


class SearchDebugTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(search_debug.search_debug_enabled())

    def test_enabled_with_post_debug_search(self) -> None:
        with mock.patch.dict(os.environ, {"POST_DEBUG_SEARCH": "1"}, clear=True):
            self.assertTrue(search_debug.search_debug_enabled())

    def test_trace_noop_when_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(search_debug._logger, "debug") as debug_log:
                search_debug.search_trace("event", value=1)
        debug_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
