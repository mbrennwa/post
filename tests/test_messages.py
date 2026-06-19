# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import paginate_messages, sort_messages_newest_first


class SortMessagesNewestFirstTests(unittest.TestCase):
    def test_newest_on_top(self) -> None:
        messages = [
            {"uid": "1", "subject": "Old", "sort_date": 100},
            {"uid": "2", "subject": "New", "sort_date": 300},
            {"uid": "3", "subject": "Mid", "sort_date": 200},
        ]
        sorted_messages = sort_messages_newest_first(messages)
        self.assertEqual([m["uid"] for m in sorted_messages], ["2", "3", "1"])

    def test_prefers_received_over_sent_when_sorting(self) -> None:
        messages = [
            {"uid": "1", "sort_date": 500},
            {"uid": "2", "sort_date": 0},
        ]
        sorted_messages = sort_messages_newest_first(messages)
        self.assertEqual(sorted_messages[0]["uid"], "1")

    def test_does_not_mutate_input(self) -> None:
        messages = [{"uid": "1", "sort_date": 1}, {"uid": "2", "sort_date": 2}]
        original = list(messages)
        sort_messages_newest_first(messages)
        self.assertEqual(messages, original)


class PaginateMessagesTests(unittest.TestCase):
    def test_first_page(self) -> None:
        messages = [{"uid": str(index)} for index in range(5)]
        page, has_more = paginate_messages(messages, offset=0, limit=2)
        self.assertEqual([m["uid"] for m in page], ["0", "1"])
        self.assertTrue(has_more)

    def test_middle_page(self) -> None:
        messages = [{"uid": str(index)} for index in range(5)]
        page, has_more = paginate_messages(messages, offset=2, limit=2)
        self.assertEqual([m["uid"] for m in page], ["2", "3"])
        self.assertTrue(has_more)

    def test_last_page(self) -> None:
        messages = [{"uid": str(index)} for index in range(5)]
        page, has_more = paginate_messages(messages, offset=4, limit=2)
        self.assertEqual([m["uid"] for m in page], ["4"])
        self.assertFalse(has_more)

    def test_empty(self) -> None:
        page, has_more = paginate_messages([], offset=0, limit=50)
        self.assertEqual(page, [])
        self.assertFalse(has_more)


if __name__ == "__main__":
    unittest.main()
