# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.calendar_dialog import default_calendar_index
from post.mail.calendar_write import CalendarTarget


class DefaultCalendarIndexTests(unittest.TestCase):
    def test_hit_selects_remembered_uid(self) -> None:
        targets = [
            CalendarTarget(uid="cal-a", display_name="Alpha"),
            CalendarTarget(uid="cal-b", display_name="Beta"),
        ]
        self.assertEqual(default_calendar_index(targets, "cal-b"), 1)

    def test_miss_and_none_use_first(self) -> None:
        targets = [
            CalendarTarget(uid="cal-a", display_name="Alpha"),
            CalendarTarget(uid="cal-b", display_name="Beta"),
        ]
        self.assertEqual(default_calendar_index(targets, "gone"), 0)
        self.assertEqual(default_calendar_index(targets, None), 0)

    def test_empty_targets_use_zero(self) -> None:
        self.assertEqual(default_calendar_index([], "cal-a"), 0)
        self.assertEqual(default_calendar_index([], None), 0)


if __name__ == "__main__":
    unittest.main()
