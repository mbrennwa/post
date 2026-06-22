# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from post.mail.helpers import (
    enrich_message_dict_from_mime,
    flag_menu_items,
    flag_menu_label,
    format_attachment_size,
    format_message_datetime,
    format_message_header,
    format_message_list_date,
    format_recipient_header,
    message_has_attachments,
    message_info_to_dict,
    message_is_flagged,
    message_is_read_unflagged,
    message_is_unread,
    paginate_messages,
    read_menu_items,
    read_menu_label,
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

    def test_includes_reply_to_when_different_from_from(self) -> None:
        header = format_message_header(
            {
                "from": "Newsletters <mbrennwa@gmail.com>",
                "reply_to": "Test Author <matthias@brennwald.org>",
                "to": "mbrennwa@gmail.com",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertIn("From: Newsletters <mbrennwa@gmail.com>", header)
        self.assertIn("Reply-To: Test Author <matthias@brennwald.org>", header)
        lines = header.splitlines()
        self.assertEqual(lines[0], "From: Newsletters <mbrennwa@gmail.com>")
        self.assertEqual(lines[1], "Reply-To: Test Author <matthias@brennwald.org>")

    def test_omits_reply_to_when_same_address_as_from(self) -> None:
        header = format_message_header(
            {
                "from": "Alice <alice@example.com>",
                "reply_to": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertNotIn("Reply-To:", header)

    def test_omits_reply_to_when_absent(self) -> None:
        header = format_message_header(
            {
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertNotIn("Reply-To:", header)


class FormatMessageDatetimeTests(unittest.TestCase):
    def test_space_separated(self) -> None:
        # 2026-06-19 12:00:00 UTC
        value = format_message_datetime(1750324800)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("T", value or "")

    def test_invalid_timestamp_returns_none(self) -> None:
        self.assertIsNone(format_message_datetime(0))
        self.assertIsNone(format_message_datetime(-1))
        self.assertIsNone(format_message_datetime(999999999999999))


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

    def test_read_unflagged_flag(self) -> None:
        self.assertTrue(
            message_is_read_unflagged({"flags": {"seen": True, "flagged": False}})
        )
        self.assertFalse(
            message_is_read_unflagged({"flags": {"seen": True, "flagged": True}})
        )
        self.assertFalse(
            message_is_read_unflagged({"flags": {"seen": False, "flagged": False}})
        )


class MessageMenuItemsTests(unittest.TestCase):
    def test_read_menu_all_unread(self) -> None:
        self.assertEqual(read_menu_items([False, False]), ["read"])

    def test_read_menu_all_read(self) -> None:
        self.assertEqual(read_menu_items([True, True]), ["unread"])

    def test_read_menu_mixed(self) -> None:
        self.assertEqual(read_menu_items([True, False]), ["read", "unread"])

    def test_flag_menu_all_unflagged(self) -> None:
        self.assertEqual(flag_menu_items([False, False]), ["flag"])

    def test_flag_menu_all_flagged(self) -> None:
        self.assertEqual(flag_menu_items([True, True]), ["unflag"])

    def test_flag_menu_mixed(self) -> None:
        self.assertEqual(flag_menu_items([True, False]), ["flag", "unflag"])

    def test_read_menu_labels_include_count(self) -> None:
        self.assertEqual(read_menu_label("read", 3), "Mark as Read (3)")
        self.assertEqual(read_menu_label("unread", 1), "Mark as Unread")

    def test_flag_menu_labels_include_count(self) -> None:
        self.assertEqual(flag_menu_label("flag", 2), "Flag (2)")
        self.assertEqual(flag_menu_label("unflag", 1), "Unflag")


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


class FormatRecipientHeaderTests(unittest.TestCase):
    def test_plain_string(self) -> None:
        self.assertEqual(format_recipient_header("a@b.com"), "a@b.com")

    def test_camel_internet_address(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        addresses = Camel.InternetAddress.new()
        addresses.add("Carol", "carol@example.com")
        addresses.add("Dave", "dave@example.com")
        self.assertEqual(
            format_recipient_header(addresses),
            "Carol <carol@example.com>, Dave <dave@example.com>",
        )


class MessageInfoToDictTests(unittest.TestCase):
    def test_formats_camel_cc_address(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        cc = Camel.InternetAddress.new()
        cc.add("Carol", "carol@example.com")
        info = MagicMock()
        info.get_uid.return_value = "1"
        info.get_subject.return_value = "Hi"
        info.get_from.return_value = "Alice <alice@example.com>"
        info.get_to.return_value = "Bob <bob@example.com>"
        info.get_cc.return_value = cc
        info.get_date_sent.return_value = 1_700_000_000
        info.get_date_received.return_value = 1_700_000_100
        info.get_flags.return_value = 0
        info.get_size.return_value = 100
        result = message_info_to_dict(info)
        self.assertEqual(result["cc"], "Carol <carol@example.com>")


class EnrichMessageDictFromMimeTests(unittest.TestCase):
    def test_fills_missing_cc(self) -> None:
        result = {"to": "Bob <bob@example.com>"}
        mime = MagicMock()
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": "Carol <carol@example.com>",
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["cc"], "Carol <carol@example.com>")

    def test_fills_reply_to(self) -> None:
        result = {"from": "List <list@example.com>"}
        mime = MagicMock()
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": None,
            "Reply-To": "Author <author@example.com>",
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["reply_to"], "Author <author@example.com>")


if __name__ == "__main__":
    unittest.main()
