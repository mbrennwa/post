# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import (
    format_attachment_size,
    format_message_datetime,
    format_message_header,
    format_message_list_date,
    message_has_attachments,
    message_is_flagged,
    message_is_unread,
    paginate_messages,
    sort_messages_newest_first,
)


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


class FormatMessageHeaderTests(unittest.TestCase):
    def test_includes_to_and_date(self) -> None:
        header = format_message_header(
            {
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "cc": "",
                "date_received": "2026-06-19 14:30:00",
            }
        )
        self.assertEqual(
            header,
            "From: Alice <alice@example.com>\n"
            "To: Bob <bob@example.com>\n"
            "Date: 2026-06-19 14:30:00",
        )

    def test_includes_cc_when_present(self) -> None:
        header = format_message_header(
            {
                "from": "Alice",
                "to": "Bob",
                "cc": "Carol <carol@example.com>",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("CC: Carol <carol@example.com>", header)

    def test_omits_cc_when_empty(self) -> None:
        header = format_message_header(
            {"from": "Alice", "to": "Bob", "cc": "  ", "date_sent": "2026-06-19 14:30:00"}
        )
        self.assertNotIn("CC:", header)


class FormatMessageDatetimeTests(unittest.TestCase):
    def test_space_separated(self) -> None:
        # 2026-06-19 12:00:00 UTC
        value = format_message_datetime(1750324800)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("T", value or "")


class MessageFlagTests(unittest.TestCase):
    def test_unread_when_not_seen(self) -> None:
        self.assertTrue(message_is_unread({"flags": {"seen": False}}))
        self.assertFalse(message_is_unread({"flags": {"seen": True}}))

    def test_attachments_flag(self) -> None:
        self.assertTrue(message_has_attachments({"flags": {"attachments": True}}))
        self.assertFalse(message_has_attachments({"flags": {"attachments": False}}))

    def test_flagged_flag(self) -> None:
        self.assertTrue(message_is_flagged({"flags": {"flagged": True}}))
        self.assertFalse(message_is_flagged({"flags": {"flagged": False}}))


class FormatMessageListDateTests(unittest.TestCase):
    def test_truncates_to_minutes(self) -> None:
        self.assertEqual(
            format_message_list_date({"date_received": "2026-06-19 14:30:00"}),
            "2026-06-19 14:30",
        )

    def test_falls_back_to_sort_date(self) -> None:
        value = format_message_list_date({"sort_date": 1750324800})
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


class FormatAttachmentSizeTests(unittest.TestCase):
    def test_bytes(self) -> None:
        self.assertEqual(format_attachment_size(512), "512 B")

    def test_kilobytes(self) -> None:
        self.assertEqual(format_attachment_size(2048), "2.0 KB")

    def test_unknown(self) -> None:
        self.assertEqual(format_attachment_size(None), "")


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
