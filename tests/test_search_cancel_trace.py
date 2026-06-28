# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import unittest
from unittest import mock

import post.search_cancel_trace as search_cancel_trace


class SearchCancelTraceTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with mock.patch.object(search_cancel_trace, "_ENABLED", False):
            self.assertFalse(search_cancel_trace.enabled())

    def test_trace_emits_when_enabled(self) -> None:
        logger = logging.getLogger("post.search-cancel")
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(search_cancel_trace, "_ENABLED", True):
                with self.assertLogs("post.search-cancel", level="DEBUG") as captured:
                    search_cancel_trace.trace("unit_test", uid="42", read_id=7)
                self.assertTrue(
                    any(
                        "unit_test" in line and "uid='42'" in line
                        for line in captured.output
                    )
                )
        finally:
            logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
