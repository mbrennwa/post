# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock

from post.mail.folders import is_sent_folder_name
from post.mail.helpers import (
    _decode_attachment_filename,
    _decode_header_value,
    enrich_message_dict_from_mime,
    flag_menu_items,
    flag_menu_label,
    format_attachment_size,
    format_message_datetime,
    format_forward_quote_header,
    format_reader_header,
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
    reader_toggle_button_state,
    should_offer_send_again,
    sort_messages_newest_first,
)
from post.mail.search import annotate_search_match


@contextmanager
def fixed_timezone(tz_name: str):
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


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


class FormatReaderHeaderTests(unittest.TestCase):
    def test_includes_to_and_date(self) -> None:
        header = format_reader_header(
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
        header = format_reader_header(
            {
                "from": "Alice",
                "to": "Bob",
                "cc": "Carol <carol@example.com>",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("CC: Carol <carol@example.com>", header)

    def test_omits_cc_when_empty(self) -> None:
        header = format_reader_header(
            {"from": "Alice", "to": "Bob", "cc": "  ", "date_sent": "2026-06-19 14:30:00"}
        )
        self.assertNotIn("CC:", header)

    def test_includes_reply_to_when_different_from_from(self) -> None:
        header = format_reader_header(
            {
                "from": "Newsletters <newsletters@example.com>",
                "reply_to": "Test Author <author@example.org>",
                "to": "owner@example.com",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertIn("From: Newsletters <newsletters@example.com>", header)
        self.assertIn("Reply-To: Test Author <author@example.org>", header)
        lines = header.splitlines()
        self.assertEqual(lines[0], "From: Newsletters <newsletters@example.com>")
        self.assertEqual(lines[1], "Reply-To: Test Author <author@example.org>")

    def test_omits_reply_to_when_same_address_as_from(self) -> None:
        header = format_reader_header(
            {
                "from": "Alice <alice@example.com>",
                "reply_to": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertNotIn("Reply-To:", header)

    def test_omits_reply_to_when_absent(self) -> None:
        header = format_reader_header(
            {
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "date_received": "2026-06-22 19:31:58",
            }
        )
        self.assertNotIn("Reply-To:", header)

    def test_includes_bcc_when_present(self) -> None:
        header = format_reader_header(
            {
                "from": "Alice",
                "to": "Bob",
                "bcc": "Dave <dave@example.com>",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("Bcc: Dave <dave@example.com>", header)

    def test_omits_bcc_when_empty(self) -> None:
        header = format_reader_header(
            {"from": "Alice", "to": "Bob", "bcc": "  ", "date_sent": "2026-06-19 14:30:00"}
        )
        self.assertNotIn("Bcc:", header)


class FormatForwardQuoteHeaderTests(unittest.TestCase):
    def test_omits_bcc_when_present(self) -> None:
        header = format_forward_quote_header(
            {
                "from": "Alice",
                "to": "Bob",
                "bcc": "Dave <dave@example.com>",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertNotIn("Bcc:", header)
        self.assertNotIn("dave@example.com", header)
        self.assertIn("From: Alice", header)
        self.assertIn("To: Bob", header)

    def test_includes_cc_when_present(self) -> None:
        header = format_forward_quote_header(
            {
                "from": "Alice",
                "to": "Bob",
                "cc": "Carol <carol@example.com>",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("CC: Carol <carol@example.com>", header)


class FormatMessageDatetimeTests(unittest.TestCase):
    def test_space_separated(self) -> None:
        value = format_message_datetime(1750324800)
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertNotIn("T", value or "")

    def test_formats_in_local_timezone_summer(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            self.assertEqual(
                format_message_datetime(1750324800),
                "2025-06-19 11:20:00",
            )

    def test_formats_in_local_timezone_winter(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            self.assertEqual(
                format_message_datetime(1704067200),
                "2024-01-01 01:00:00",
            )

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

    def test_send_again_single_sent(self) -> None:
        self.assertTrue(
            should_offer_send_again(selection_count=1, source_is_sent=True)
        )

    def test_send_again_single_not_sent(self) -> None:
        self.assertFalse(
            should_offer_send_again(selection_count=1, source_is_sent=False)
        )

    def test_send_again_multi_select_sent(self) -> None:
        self.assertFalse(
            should_offer_send_again(selection_count=2, source_is_sent=True)
        )

    def test_send_again_uses_annotated_search_folder_not_sidebar(self) -> None:
        """Search hits carry their source folder; sidebar may still be Inbox."""
        sent_match = annotate_search_match(
            {"uid": "42", "subject": "hello"},
            account_uid="acct-1",
            folder_name="Sent",
        )
        inbox_match = annotate_search_match(
            {"uid": "7", "subject": "other"},
            account_uid="acct-1",
            folder_name="INBOX",
        )
        # Location resolution prefers search annotations over current folder.
        cases = (
            (sent_match, True),
            (inbox_match, False),
        )
        for message, expect_offer in cases:
            folder_name = str(message["_search_folder"])
            source_is_sent = is_sent_folder_name([], folder_name)
            self.assertEqual(
                should_offer_send_again(
                    selection_count=1, source_is_sent=source_is_sent
                ),
                expect_offer,
                msg=f"folder={folder_name!r}",
            )
        # Sent search hit must not depend on sidebar still being Inbox.
        self.assertEqual(sent_match["_search_folder"], "Sent")
        self.assertNotEqual(sent_match["_search_folder"], "INBOX")


class FetchAttachmentSearchLocationTests(unittest.TestCase):
    """#180: attachment fetch must use search-hit location, not sidebar."""

    def _window_for_search_hit(self, message: dict, *, sidebar_folder: str):
        from types import SimpleNamespace
        from unittest import mock

        from post.window import MainWindow

        account = SimpleNamespace(uid="acct-1")
        window = SimpleNamespace(
            _current_account=account,
            _current_folder=sidebar_folder,
            _current_folder_messages=[message],
            _current_message_uid=str(message["_search_row_key"]),
            _mail=mock.Mock(),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._on_attachment_fetched = (
            lambda filename, data, error, on_ready: MainWindow._on_attachment_fetched(
                window, filename, data, error, on_ready
            )
        )
        return window

    def test_location_prefers_annotated_folder_over_sidebar(self) -> None:
        from post.window import MainWindow

        sent_match = annotate_search_match(
            {"uid": "42", "subject": "with pdf"},
            account_uid="acct-1",
            folder_name="Sent",
        )
        window = self._window_for_search_hit(sent_match, sidebar_folder="INBOX")
        location = MainWindow._message_location_for_list_key(
            window, window._current_message_uid
        )
        self.assertEqual(location, ("acct-1", "Sent", "42"))
        self.assertNotEqual(location[1], window._current_folder)
        self.assertNotIn("\0", location[2])

    def test_location_same_folder_search_still_uses_plain_uid(self) -> None:
        from post.window import MainWindow

        inbox_match = annotate_search_match(
            {"uid": "7", "subject": "inbox hit"},
            account_uid="acct-1",
            folder_name="INBOX",
        )
        window = self._window_for_search_hit(inbox_match, sidebar_folder="INBOX")
        location = MainWindow._message_location_for_list_key(
            window, window._current_message_uid
        )
        self.assertEqual(location, ("acct-1", "INBOX", "7"))
        self.assertEqual(window._current_message_uid, inbox_match["_search_row_key"])
        self.assertNotEqual(location[2], window._current_message_uid)

    def test_fetch_attachment_passes_resolved_location_not_list_key(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        sent_match = annotate_search_match(
            {"uid": "42", "subject": "with pdf"},
            account_uid="acct-1",
            folder_name="Sent",
        )
        window = self._window_for_search_hit(sent_match, sidebar_folder="INBOX")
        window._mail.read_attachment_data.return_value = ("file.pdf", b"%PDF")

        class _ImmediateMailIoThread:
            def submit(self, func, /, *args, **kwargs) -> None:
                func(*args, **kwargs)

        def _run_idle_add(func, *args):
            func(*args)
            return False

        on_ready = mock.Mock()
        with (
            mock.patch(
                "post.window.get_mail_io_thread",
                return_value=_ImmediateMailIoThread(),
            ),
            mock.patch("post.window.GLib.idle_add", side_effect=_run_idle_add),
        ):
            MainWindow._fetch_attachment(window, 0, on_ready)

        window._mail.read_attachment_data.assert_called_once_with(
            "acct-1", "Sent", "42", 0
        )
        on_ready.assert_called_once_with("file.pdf", b"%PDF", None)


class ReaderToggleButtonStateTests(unittest.TestCase):
    def test_read_flagged_shows_unread_and_unflag_actions(self) -> None:
        state = reader_toggle_button_state({"seen": True, "flagged": True})
        self.assertEqual(state["read"]["icon"], "mail-unread-symbolic")
        self.assertEqual(state["read"]["tooltip"], "Mark as Unread")
        self.assertFalse(state["read"]["styled_action"])
        self.assertEqual(state["flag"]["icon"], "mail-flag-symbolic")
        self.assertEqual(state["flag"]["tooltip"], "Unflag")
        self.assertFalse(state["flag"]["styled_action"])

    def test_unread_unflagged_shows_read_and_flag_actions(self) -> None:
        state = reader_toggle_button_state({"seen": False, "flagged": False})
        self.assertEqual(state["read"]["icon"], "mail-mark-read-symbolic")
        self.assertEqual(state["read"]["tooltip"], "Mark as Read")
        self.assertTrue(state["read"]["styled_action"])
        self.assertEqual(state["flag"]["icon"], "mail-flag-symbolic")
        self.assertEqual(state["flag"]["tooltip"], "Flag")
        self.assertTrue(state["flag"]["styled_action"])


class FormatMessageListDateTests(unittest.TestCase):
    def test_truncates_to_minutes(self) -> None:
        self.assertEqual(
            format_message_list_date({"date_received": "2026-06-19 14:30:00"}),
            "2026-06-19 14:30",
        )

    def test_falls_back_to_sort_date(self) -> None:
        value = format_message_list_date({"sort_date": 1750324800})
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_falls_back_to_sort_date_in_local_timezone(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            self.assertEqual(
                format_message_list_date({"sort_date": 1750324800}),
                "2025-06-19 11:20",
            )


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

    def test_empty_camel_internet_address(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        self.assertEqual(format_recipient_header(Camel.InternetAddress.new()), "")


class DecodeHeaderValueTests(unittest.TestCase):
    def test_rfc2047_encoded_word_as_str(self) -> None:
        self.assertEqual(
            _decode_header_value("=?ISO-8859-1?Q?Gr=FC=DFe?="),
            "Grüße",
        )

    def test_rfc2047_encoded_word_as_bytes(self) -> None:
        self.assertEqual(
            _decode_header_value(b"=?ISO-8859-1?Q?Gr=FC=DFe?="),
            "Grüße",
        )

    def test_already_decoded_unicode_unchanged(self) -> None:
        self.assertEqual(_decode_header_value("Grüße"), "Grüße")
        self.assertEqual(_decode_header_value("Hello"), "Hello")

    def test_raw_latin1_bytes_without_encoded_word(self) -> None:
        self.assertEqual(
            _decode_header_value("Gr\xfc\xdfe".encode("latin-1")),
            "Grüße",
        )

    def test_attachment_filename_encoded_word(self) -> None:
        self.assertEqual(
            _decode_header_value(b"=?ISO-8859-1?Q?r=E9sum=E9.pdf?="),
            "résumé.pdf",
        )

    def test_ascii_message_id_bytes(self) -> None:
        self.assertEqual(
            _decode_header_value(b"<abc@example.com>"),
            "<abc@example.com>",
        )

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_decode_header_value(None))


class DecodeAttachmentFilenameTests(unittest.TestCase):
    def test_rfc5987_utf8_filename(self) -> None:
        self.assertEqual(
            _decode_attachment_filename("utf-8''r%C3%A9sum%C3%A9.pdf"),
            "résumé.pdf",
        )

    def test_percent_encoded_without_charset_prefix(self) -> None:
        self.assertEqual(
            _decode_attachment_filename("r%C3%A9sum%C3%A9.pdf"),
            "résumé.pdf",
        )

    def test_rfc2047_encoded_word(self) -> None:
        self.assertEqual(
            _decode_attachment_filename(b"=?ISO-8859-1?Q?r=E9sum=E9.pdf?="),
            "résumé.pdf",
        )

    def test_already_decoded_unicode_unchanged(self) -> None:
        self.assertEqual(_decode_attachment_filename("Grüße.txt"), "Grüße.txt")
        self.assertEqual(_decode_attachment_filename("doc.pdf"), "doc.pdf")

    def test_none_returns_none(self) -> None:
        self.assertIsNone(_decode_attachment_filename(None))


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

    def test_decodes_rfc2047_subject_bytes(self) -> None:
        info = MagicMock()
        info.get_uid.return_value = "1"
        info.get_subject.return_value = b"=?ISO-8859-1?Q?Gr=FC=DFe?="
        info.get_from.return_value = "Alice <alice@example.com>"
        info.get_to.return_value = "Bob <bob@example.com>"
        info.get_cc.return_value = None
        info.get_date_sent.return_value = 1_700_000_000
        info.get_date_received.return_value = 1_700_000_100
        info.get_flags.return_value = 0
        info.get_size.return_value = 100
        result = message_info_to_dict(info)
        self.assertEqual(result["subject"], "Grüße")


class EnrichMessageDictFromMimeTests(unittest.TestCase):
    def test_fills_missing_cc(self) -> None:
        result = {"to": "Bob <bob@example.com>"}
        mime = MagicMock()
        mime.get_recipients.return_value = None
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": "Carol <carol@example.com>",
            "Bcc": None,
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["cc"], "Carol <carol@example.com>")

    def test_overwrites_partial_to_from_mime(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        mime = Camel.MimeMessage()
        to = Camel.InternetAddress.new()
        to.add("Alice", "alice@example.com")
        to.add("Bob", "bob@example.com")
        mime.set_recipients("to", to)

        result = {"to": "alice@example.com"}
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(
            result["to"],
            "Alice <alice@example.com>, Bob <bob@example.com>",
        )

    def test_fills_bcc_from_mime(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        mime = Camel.MimeMessage()
        bcc = Camel.InternetAddress.new()
        bcc.add("Dave", "dave@example.com")
        mime.set_recipients("bcc", bcc)

        result: dict[str, str] = {}
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["bcc"], "Dave <dave@example.com>")

    def test_ignores_empty_recipient_containers(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        mime = Camel.MimeMessage()
        to = Camel.InternetAddress.new()
        to.add("Alice", "alice@example.com")
        mime.set_recipients("to", to)
        mime.set_recipients("cc", Camel.InternetAddress.new())
        mime.set_recipients("bcc", Camel.InternetAddress.new())

        result = {"cc": "stale", "bcc": "stale"}
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["to"], "Alice <alice@example.com>")
        self.assertEqual(result["cc"], "stale")
        self.assertEqual(result["bcc"], "stale")

    def test_fills_reply_to(self) -> None:
        result = {"from": "List <list@example.com>"}
        mime = MagicMock()
        mime.get_recipients.return_value = None
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": None,
            "Bcc": None,
            "Reply-To": "Author <author@example.com>",
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(result["reply_to"], "Author <author@example.com>")

    def test_fills_unsubscribe_one_click(self) -> None:
        result: dict = {}
        mime = MagicMock()
        mime.get_recipients.return_value = None
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": None,
            "Bcc": None,
            "List-Unsubscribe": (
                "<mailto:off@example.com>, <https://example.com/unsub>"
            ),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(
            result["unsubscribe"],
            {"kind": "post", "url": "https://example.com/unsub"},
        )

    def test_fills_unsubscribe_open_https(self) -> None:
        result: dict = {}
        mime = MagicMock()
        mime.get_recipients.return_value = None
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": None,
            "Bcc": None,
            "List-Unsubscribe": "<https://example.com/leave>",
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertEqual(
            result["unsubscribe"],
            {"kind": "open", "url": "https://example.com/leave"},
        )

    def test_omits_unsubscribe_without_headers(self) -> None:
        result: dict = {}
        mime = MagicMock()
        mime.get_recipients.return_value = None
        mime.get_header.side_effect = lambda name: {
            "To": None,
            "Cc": None,
            "Bcc": None,
        }.get(name)
        enrich_message_dict_from_mime(result, mime)
        self.assertNotIn("unsubscribe", result)


if __name__ == "__main__":
    unittest.main()
