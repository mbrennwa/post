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
    bare_email_from_address,
    enrich_message_dict_from_mime,
    flag_menu_items,
    flag_menu_label,
    format_attachment_size,
    format_from_search_query,
    format_message_datetime,
    format_forward_quote_header,
    format_reader_header,
    format_message_list_date,
    format_recipient_header,
    insert_messages_newest_first,
    mailto_primary_email,
    message_has_attachments,
    message_info_to_dict,
    message_is_flagged,
    message_is_read_unflagged,
    message_is_unread,
    message_matches_bulk_archive_scope,
    paginate_messages,
    read_menu_items,
    read_menu_label,
    reader_header_rows,
    reader_toggle_button_state,
    should_offer_send_again,
    sort_messages_newest_first,
    uniform_bool_state,
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


class InsertMessagesNewestFirstTests(unittest.TestCase):
    def test_inserts_batch_newest_on_top(self) -> None:
        existing = [
            {"uid": "inbox-old", "sort_date": 200},
            {"uid": "inbox-older", "sort_date": 50},
        ]
        insert_messages_newest_first(
            existing,
            [
                {"uid": "archive-new", "sort_date": 300},
                {"uid": "archive-mid", "sort_date": 100},
            ],
        )
        self.assertEqual(
            [message["uid"] for message in existing],
            ["archive-new", "inbox-old", "archive-mid", "inbox-older"],
        )

    def test_equal_dates_stay_after_existing(self) -> None:
        existing = [{"uid": "a", "sort_date": 100}]
        insert_messages_newest_first(existing, [{"uid": "b", "sort_date": 100}])
        self.assertEqual([message["uid"] for message in existing], ["a", "b"])

    def test_empty_batch_is_noop(self) -> None:
        existing = [{"uid": "1", "sort_date": 1}]
        insert_messages_newest_first(existing, [])
        self.assertEqual([message["uid"] for message in existing], ["1"])


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
        self.assertIn("Cc: Carol <carol@example.com>", header)

    def test_omits_cc_when_empty(self) -> None:
        header = format_reader_header(
            {"from": "Alice", "to": "Bob", "cc": "  ", "date_sent": "2026-06-19 14:30:00"}
        )
        self.assertNotIn("Cc:", header)

    def test_includes_subject_when_present(self) -> None:
        header = format_reader_header(
            {
                "from": "Alice",
                "to": "Bob",
                "subject": "Hello there",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("Subject: Hello there", header)

    def test_omits_subject_when_empty(self) -> None:
        header = format_reader_header(
            {
                "from": "Alice",
                "to": "Bob",
                "subject": "  ",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertNotIn("Subject:", header)

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


class AddressContextHelperTests(unittest.TestCase):
    def test_format_from_search_query_plain(self) -> None:
        self.assertEqual(
            format_from_search_query("user@example.com"),
            "from: user@example.com",
        )

    def test_format_from_search_query_quotes_spaces(self) -> None:
        self.assertEqual(
            format_from_search_query('Alice "Ada" <a@example.com>'),
            'from: "Alice \\"Ada\\" <a@example.com>"',
        )

    def test_bare_email_from_address(self) -> None:
        self.assertEqual(
            bare_email_from_address("Alice <alice@example.com>"),
            "alice@example.com",
        )

    def test_mailto_primary_email(self) -> None:
        self.assertEqual(
            mailto_primary_email("mailto:Alice%20%3Calice@example.com%3E"),
            "alice@example.com",
        )
        self.assertEqual(mailto_primary_email("https://example.com"), "")

    def test_reader_header_rows_addresses_and_date(self) -> None:
        rows = reader_header_rows(
            {
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>, Carol <carol@example.com>",
                "cc": "Dave <dave@example.com>",
                "date_received": "2026-06-19 14:30:00",
            }
        )
        by_label = {row.label: row for row in rows}
        self.assertEqual(by_label["From"].addresses, ("Alice <alice@example.com>",))
        self.assertEqual(len(by_label["To"].addresses), 2)
        self.assertEqual(by_label["Cc"].addresses, ("Dave <dave@example.com>",))
        self.assertEqual(by_label["Date"].plain, "2026-06-19 14:30:00")
        self.assertEqual(by_label["Date"].addresses, ())


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
        self.assertIn("Cc: Carol <carol@example.com>", header)

    def test_includes_subject_when_present(self) -> None:
        header = format_forward_quote_header(
            {
                "from": "Alice",
                "to": "Bob",
                "subject": "Meeting notes",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertIn("Subject: Meeting notes", header)
        lines = header.splitlines()
        self.assertEqual(lines[-1], "Subject: Meeting notes")

    def test_omits_subject_when_empty(self) -> None:
        header = format_forward_quote_header(
            {
                "from": "Alice",
                "to": "Bob",
                "subject": "  ",
                "date_sent": "2026-06-19 14:30:00",
            }
        )
        self.assertNotIn("Subject:", header)


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

    def test_bulk_archive_scope_matching(self) -> None:
        unread = {"uid": "1", "flags": {"seen": False, "flagged": False}}
        read = {"uid": "2", "flags": {"seen": True, "flagged": False}}
        flagged = {"uid": "3", "flags": {"seen": True, "flagged": True}}
        self.assertTrue(message_matches_bulk_archive_scope(unread, "all"))
        self.assertTrue(message_matches_bulk_archive_scope(read, "all"))
        self.assertFalse(message_matches_bulk_archive_scope(unread, "read"))
        self.assertTrue(message_matches_bulk_archive_scope(read, "read"))
        self.assertTrue(message_matches_bulk_archive_scope(flagged, "read"))
        self.assertTrue(message_matches_bulk_archive_scope(read, "read_unflagged"))
        self.assertFalse(message_matches_bulk_archive_scope(flagged, "read_unflagged"))
        self.assertFalse(message_matches_bulk_archive_scope(unread, "read_unflagged"))
        self.assertFalse(message_matches_bulk_archive_scope(read, "unknown"))


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


class SearchSelectionReaderSyncTests(unittest.TestCase):
    """Search list selection must load the clicked hit, not a stale reader row."""

    def _window_stub(self, *, messages: list[dict], sidebar_folder: str = "Archive"):
        from types import SimpleNamespace
        from unittest import mock

        from post.window import MainWindow

        account = SimpleNamespace(uid="acct-1")
        window = SimpleNamespace(
            _current_account=account,
            _current_folder=sidebar_folder,
            _current_folder_messages=list(messages),
            _current_message_uid=None,
            _current_message=None,
            _pending_restore_message_uid="old-plain-uid",
            _pending_message_read_uid=None,
            _inflight_message_read_id=None,
            _message_read_generation=0,
            _user_message_click_pending=False,
            _mark_seen_intent_list_key=None,
            _mail=mock.Mock(),
            _reader_pane=mock.Mock(),
            _sidebar=SimpleNamespace(
                folder_is_drafts=lambda _account_uid, folder: folder == "Drafts",
            ),
            _message_list_view=mock.Mock(
                get_selected_uids=mock.Mock(return_value=["selected-key"]),
            ),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._loaded_message_source_location = (
            lambda: MainWindow._loaded_message_source_location(window)
        )
        window._reader_shows_list_key = lambda list_key: MainWindow._reader_shows_list_key(
            window, list_key
        )
        window._mark_seen_when_reading_uid = (
            lambda list_key: MainWindow._mark_seen_when_reading_uid(window, list_key)
        )
        window._load_message_body_for_uid = mock.Mock()
        return window

    def test_reader_shows_list_key_false_when_stale_body(self) -> None:
        from post.window import MainWindow

        hit = annotate_search_match(
            {"uid": "100", "subject": "Prusa repair"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        stale = annotate_search_match(
            {"uid": "200", "subject": "Schau event"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        window = self._window_stub(messages=[hit, stale])
        window._current_message_uid = hit["_search_row_key"]
        window._current_message = dict(stale)

        self.assertFalse(
            MainWindow._reader_shows_list_key(window, hit["_search_row_key"])
        )
        self.assertTrue(
            MainWindow._reader_shows_list_key(window, stale["_search_row_key"])
        )

    def test_item_pressed_reloads_when_uid_matches_but_reader_stale(self) -> None:
        from post.window import MainWindow

        hit = annotate_search_match(
            {"uid": "100", "subject": "Prusa repair"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        stale = annotate_search_match(
            {"uid": "200", "subject": "Schau event"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        window = self._window_stub(messages=[hit, stale])
        list_key = hit["_search_row_key"]
        window._message_list_view.get_selected_uids.return_value = [list_key]
        window._current_message_uid = list_key
        window._current_message = dict(stale)

        MainWindow._on_message_list_item_pressed(window, list_key)

        window._load_message_body_for_uid.assert_called_once_with(
            list_key, mark_seen=True
        )
        self.assertEqual(window._mark_seen_intent_list_key, list_key)
        self.assertIsNone(window._pending_restore_message_uid)

    def test_selection_changed_idle_reloads_stale_reader(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        hit = annotate_search_match(
            {"uid": "100", "subject": "Prusa repair"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        stale = annotate_search_match(
            {"uid": "200", "subject": "Schau event"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        window = self._window_stub(messages=[hit, stale])
        list_key = hit["_search_row_key"]
        window._message_list_view.get_selected_uids.return_value = [list_key]
        window._message_list_view.is_restoring_selection = mock.Mock(
            return_value=False
        )
        window._update_message_toolbar = mock.Mock()
        window._current_message_uid = list_key
        window._current_message = dict(stale)

        MainWindow._on_message_list_selection_changed_idle(window)

        window._load_message_body_for_uid.assert_called_once_with(
            list_key, mark_seen=False
        )

    def test_reader_shows_list_key_false_for_search_key_without_annotations(
        self,
    ) -> None:
        from post.window import MainWindow

        archive_hit = annotate_search_match(
            {"uid": "100", "subject": "Archive thread"},
            account_uid="acct-1",
            folder_name="Archive",
        )
        window = self._window_stub(messages=[archive_hit], sidebar_folder="INBOX")
        window._current_message = {"uid": "100", "subject": "Inbox notification"}

        self.assertFalse(
            MainWindow._reader_shows_list_key(
                window, archive_hit["_search_row_key"]
            )
        )

    def test_apply_search_matches_reconciles_reader(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from post.window import MainWindow

        hit = annotate_search_match(
            {"uid": "100", "subject": "Search hit", "sort_date": 300},
            account_uid="acct-1",
            folder_name="Archive",
        )
        stale = annotate_search_match(
            {"uid": "200", "subject": "Stale reader", "sort_date": 100},
            account_uid="acct-1",
            folder_name="INBOX",
        )
        account = SimpleNamespace(uid="acct-1")
        list_key = hit["_search_row_key"]
        window = SimpleNamespace(
            _is_closing=False,
            _messages_load_generation=1,
            _search_query=object(),
            _current_account=account,
            _current_folder="INBOX",
            _search_results_streamed=False,
            _current_folder_messages=[stale],
            _current_message_uid=list_key,
            _current_message=dict(stale),
            _mark_seen_intent_list_key=None,
            _pending_message_read_uid=None,
            _inflight_message_read_id=None,
            _message_stack=mock.Mock(
                get_visible_child_name=mock.Mock(return_value="list")
            ),
            _message_list_view=mock.Mock(
                get_selected_uids=mock.Mock(return_value=[list_key]),
                is_restoring_selection=mock.Mock(return_value=False),
                insert_messages_newest_first=mock.Mock(),
            ),
            _update_search_scope_ui=mock.Mock(),
            _load_message_body_for_uid=mock.Mock(),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._reader_shows_list_key = lambda list_key: MainWindow._reader_shows_list_key(
            window, list_key
        )
        window._ensure_reader_matches_selection = (
            lambda **kwargs: MainWindow._ensure_reader_matches_selection(
                window, **kwargs
            )
        )
        window._mark_seen_when_reading_uid = lambda _uid: True

        MainWindow._apply_search_matches(window, 1, [hit])

        window._load_message_body_for_uid.assert_called_once_with(
            list_key, mark_seen=False
        )

    def test_location_parses_search_row_key_before_sidebar_fallback(self) -> None:
        from post.mail.search import make_search_row_key
        from post.window import MainWindow

        sent_key = make_search_row_key("acct-1", "Sent", "1")
        window = self._window_stub(messages=[], sidebar_folder="INBOX")
        location = MainWindow._message_location_for_list_key(window, sent_key)
        self.assertEqual(location, ("acct-1", "Sent", "1"))


class MarkReadOnClickTests(unittest.TestCase):
    """Clicking an already-displayed unread row must mark read without reload (#377)."""

    def _window_stub(self, *, message: dict, sidebar_folder: str = "INBOX"):
        from types import SimpleNamespace
        from unittest import mock

        from post.window import MainWindow

        account = SimpleNamespace(uid="acct-1")
        uid = str(message.get("uid") or "42")
        tagged_message = {
            **message,
            "_list_account_uid": account.uid,
            "_list_folder": sidebar_folder,
        }
        window = SimpleNamespace(
            _current_account=account,
            _current_folder=sidebar_folder,
            _current_folder_messages=[message],
            _current_message_uid=uid,
            _current_message=dict(tagged_message),
            _pending_restore_message_uid=None,
            _user_message_click_pending=False,
            _mark_seen_intent_list_key=None,
            _mail=mock.Mock(),
            _reader_pane=mock.Mock(),
            _sidebar=SimpleNamespace(
                folder_is_drafts=lambda _account_uid, folder: folder == "Drafts",
            ),
            _message_list_view=mock.Mock(
                get_selected_uids=mock.Mock(return_value=[uid]),
                get_message=mock.Mock(return_value=message),
            ),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._loaded_message_source_location = (
            lambda: MainWindow._loaded_message_source_location(window)
        )
        window._reader_shows_list_key = lambda list_key: MainWindow._reader_shows_list_key(
            window, list_key
        )
        window._message_flags_for_uid = (
            lambda list_key: MainWindow._message_flags_for_uid(window, list_key)
        )
        window._mark_seen_when_reading_uid = (
            lambda list_key: MainWindow._mark_seen_when_reading_uid(
                window, list_key
            )
        )
        window._mark_message_read_on_click_if_unread = (
            lambda list_key: MainWindow._mark_message_read_on_click_if_unread(
                window, list_key
            )
        )
        window._load_message_body_for_uid = mock.Mock()
        window._set_message_flags = mock.Mock()
        window._mark_message_read = mock.Mock()
        window._message_list_view.select_uid = mock.Mock(return_value=True)
        return window

    def test_item_pressed_marks_read_when_reader_already_shows_unread(self) -> None:
        from post.window import MainWindow

        message = {
            "uid": "42",
            "subject": "Digest",
            "flags": {"seen": False},
        }
        window = self._window_stub(message=message)

        MainWindow._on_message_list_item_pressed(window, "42")

        window._mark_message_read.assert_called_once_with("42")
        window._set_message_flags.assert_called_once_with(
            "seen", seen=True, uids=["42"]
        )
        window._load_message_body_for_uid.assert_not_called()

    def test_item_pressed_loads_unselected_with_mark_seen(self) -> None:
        from post.window import MainWindow

        message = {
            "uid": "42",
            "subject": "Digest",
            "flags": {"seen": False},
        }
        window = self._window_stub(message=message)
        window._message_list_view.get_selected_uids.return_value = ["99"]
        window._current_message_uid = "99"
        window._current_message = {
            "uid": "99",
            "subject": "Other",
            "flags": {"seen": True},
        }

        MainWindow._on_message_list_item_pressed(window, "42")

        window._message_list_view.select_uid.assert_called_once_with("42")
        window._load_message_body_for_uid.assert_called_once_with(
            "42", mark_seen=True
        )
        self.assertEqual(window._mark_seen_intent_list_key, "42")
        window._set_message_flags.assert_not_called()

    def test_item_pressed_noop_when_reader_shows_read_message(self) -> None:
        from post.window import MainWindow

        message = {
            "uid": "42",
            "subject": "Digest",
            "flags": {"seen": True},
        }
        window = self._window_stub(message=message)

        MainWindow._on_message_list_item_pressed(window, "42")

        window._set_message_flags.assert_not_called()
        window._load_message_body_for_uid.assert_not_called()

    def test_item_pressed_reclick_skips_mark_in_drafts(self) -> None:
        from post.window import MainWindow

        message = {
            "uid": "42",
            "subject": "Draft",
            "flags": {"seen": False},
        }
        window = self._window_stub(message=message, sidebar_folder="Drafts")

        MainWindow._on_message_list_item_pressed(window, "42")

        window._set_message_flags.assert_not_called()
        window._load_message_body_for_uid.assert_not_called()


class MarkSeenClickIntentTests(unittest.TestCase):
    """Click mark-seen intent must survive programmatic mark_seen=False reloads (#388)."""

    def _window_stub(self, *, message: dict, sidebar_folder: str = "INBOX"):
        from types import SimpleNamespace
        from unittest import mock

        from post.window import MainWindow

        account = SimpleNamespace(uid="acct-1", email="a@b.c", display_label="a@b.c")
        uid = str(message.get("uid") or "42")
        window = SimpleNamespace(
            _current_account=account,
            _current_folder=sidebar_folder,
            _current_folder_messages=[message],
            _current_message_uid=uid,
            _current_message=None,
            _pending_restore_message_uid=None,
            _pending_message_read_uid=None,
            _inflight_message_read_id=None,
            _message_read_generation=0,
            _user_message_click_pending=False,
            _mark_seen_intent_list_key=None,
            _mail=mock.Mock(),
            _reader_pane=mock.Mock(),
            _sidebar=SimpleNamespace(
                folder_is_drafts=lambda _account_uid, folder: folder == "Drafts",
            ),
            _message_list_view=mock.Mock(
                get_selected_uids=mock.Mock(return_value=[uid]),
                is_restoring_selection=mock.Mock(return_value=False),
            ),
            _message_stack=mock.Mock(
                get_visible_child_name=mock.Mock(return_value="list")
            ),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._reader_shows_list_key = lambda list_key: MainWindow._reader_shows_list_key(
            window, list_key
        )
        window._mark_seen_when_reading_uid = (
            lambda list_key: MainWindow._mark_seen_when_reading_uid(window, list_key)
        )
        return window

    def test_ensure_reader_skips_while_click_load_in_flight(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        message = {"uid": "42", "subject": "Digest", "flags": {"seen": False}}
        window = self._window_stub(message=message)
        window._mark_seen_intent_list_key = "42"
        window._pending_message_read_uid = "42"
        window._inflight_message_read_id = 1
        window._load_message_body_for_uid = mock.Mock()

        MainWindow._ensure_reader_matches_selection(window, mark_seen=False)

        window._load_message_body_for_uid.assert_not_called()

    def test_selection_idle_skips_while_click_load_in_flight(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        message = {"uid": "42", "subject": "Digest", "flags": {"seen": False}}
        window = self._window_stub(message=message)
        window._mark_seen_intent_list_key = "42"
        window._pending_message_read_uid = "42"
        window._user_message_click_pending = True
        window._update_message_toolbar = mock.Mock()
        window._load_message_body_for_uid = mock.Mock()

        MainWindow._on_message_list_selection_changed_idle(window)

        window._load_message_body_for_uid.assert_not_called()
        self.assertFalse(window._user_message_click_pending)

    def test_load_upgrades_mark_seen_false_when_intent_and_not_inflight(self) -> None:
        from unittest import mock

        from post.window import MainWindow
        from post.mail.io_thread import get_mail_io_thread

        message = {"uid": "42", "subject": "Digest", "flags": {"seen": False}}
        window = self._window_stub(message=message)
        window._mark_seen_intent_list_key = "42"
        window._pending_message_read_uid = None
        window._inflight_message_read_id = None
        seen_kwargs: dict = {}

        def fake_read(*_args, **kwargs):
            seen_kwargs.update(kwargs)
            return {"uid": "42", "body_plain": "Hi", "flags": {"seen": True}}

        def run_front(worker) -> None:
            worker()

        window._mail.get_account = mock.Mock(return_value=window._current_account)
        window._mail.read_message = fake_read
        window._on_message_read = mock.Mock(return_value=False)
        window._on_message_read_worker_stale = mock.Mock(return_value=False)
        with mock.patch.object(
            get_mail_io_thread(), "submit_front", side_effect=run_front
        ):
            with mock.patch(
                "post.window.GLib.idle_add", side_effect=lambda *_a, **_k: False
            ):
                MainWindow._load_message_body_for_uid(window, "42", mark_seen=False)

        self.assertEqual(window._pending_message_read_uid, "42")
        self.assertTrue(seen_kwargs.get("mark_seen"))
    def test_load_skips_false_supersede_when_intent_inflight(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        message = {"uid": "42", "subject": "Digest", "flags": {"seen": False}}
        window = self._window_stub(message=message)
        window._mark_seen_intent_list_key = "42"
        window._pending_message_read_uid = "42"
        window._inflight_message_read_id = 7
        window._message_read_generation = 7
        window._mail.get_account = mock.Mock(return_value=window._current_account)

        MainWindow._load_message_body_for_uid(window, "42", mark_seen=False)

        self.assertEqual(window._message_read_generation, 7)
        self.assertEqual(window._inflight_message_read_id, 7)

    def test_apply_search_matches_skips_reload_with_click_intent_inflight(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from post.mail.search import annotate_search_match
        from post.window import MainWindow

        hit = annotate_search_match(
            {"uid": "100", "subject": "Search hit", "sort_date": 300},
            account_uid="acct-1",
            folder_name="Archive",
        )
        list_key = hit["_search_row_key"]
        account = SimpleNamespace(uid="acct-1")
        window = SimpleNamespace(
            _is_closing=False,
            _messages_load_generation=1,
            _search_query=object(),
            _current_account=account,
            _current_folder="INBOX",
            _search_results_streamed=False,
            _current_folder_messages=[],
            _current_message_uid=list_key,
            _current_message=None,
            _mark_seen_intent_list_key=list_key,
            _pending_message_read_uid=list_key,
            _inflight_message_read_id=3,
            _message_stack=mock.Mock(
                get_visible_child_name=mock.Mock(return_value="list")
            ),
            _message_list_view=mock.Mock(
                get_selected_uids=mock.Mock(return_value=[list_key]),
                is_restoring_selection=mock.Mock(return_value=False),
                insert_messages_newest_first=mock.Mock(),
            ),
            _update_search_scope_ui=mock.Mock(),
            _load_message_body_for_uid=mock.Mock(),
            _sidebar=SimpleNamespace(
                folder_is_drafts=lambda *_args: False,
            ),
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda key: MainWindow._message_location_for_list_key(window, key)
        )
        window._reader_shows_list_key = lambda key: MainWindow._reader_shows_list_key(
            window, key
        )
        window._ensure_reader_matches_selection = (
            lambda **kwargs: MainWindow._ensure_reader_matches_selection(
                window, **kwargs
            )
        )
        window._mark_seen_when_reading_uid = lambda _uid: True

        MainWindow._apply_search_matches(window, 1, [hit])

        window._load_message_body_for_uid.assert_not_called()

    def test_selection_without_click_still_loads_mark_seen_false(self) -> None:
        from unittest import mock

        from post.window import MainWindow

        message = {"uid": "42", "subject": "Digest", "flags": {"seen": False}}
        window = self._window_stub(message=message)
        window._current_message_uid = "99"
        window._mark_seen_intent_list_key = None
        window._user_message_click_pending = False
        window._update_message_toolbar = mock.Mock()
        window._load_message_body_for_uid = mock.Mock()

        MainWindow._on_message_list_selection_changed_idle(window)

        window._load_message_body_for_uid.assert_called_once_with(
            "42", mark_seen=False
        )


class ReaderListSyncTests(unittest.TestCase):
    """Reader must not treat bare IMAP uids as globally unique (#377)."""

    def _window_stub(
        self,
        *,
        messages: list[dict],
        sidebar_account: str = "acct-1",
        sidebar_folder: str = "INBOX",
    ):
        from types import SimpleNamespace

        from post.window import MainWindow

        account = SimpleNamespace(uid=sidebar_account)
        window = SimpleNamespace(
            _current_account=account,
            _current_folder=sidebar_folder,
            _current_folder_messages=list(messages),
            _current_message=None,
            _current_message_uid=None,
        )
        window._message_list_key = lambda msg: MainWindow._message_list_key(
            window, msg
        )
        window._message_location_for_list_key = (
            lambda list_key: MainWindow._message_location_for_list_key(
                window, list_key
            )
        )
        window._loaded_message_source_location = (
            lambda: MainWindow._loaded_message_source_location(window)
        )
        window._reader_shows_list_key = lambda list_key: MainWindow._reader_shows_list_key(
            window, list_key
        )
        return window

    def test_reader_shows_list_key_false_when_uid_collides_across_accounts(self) -> None:
        from post.window import MainWindow

        inbox_row = {"uid": "100", "subject": "Work quote"}
        window = self._window_stub(
            messages=[inbox_row],
            sidebar_account="acct-work",
            sidebar_folder="INBOX",
        )
        window._current_message = {
            "uid": "100",
            "subject": "Old vendor order",
            "_list_account_uid": "acct-other",
            "_list_folder": "INBOX",
        }

        self.assertFalse(MainWindow._reader_shows_list_key(window, "100"))

    def test_reader_shows_list_key_true_when_list_context_matches(self) -> None:
        from post.window import MainWindow

        inbox_row = {"uid": "100", "subject": "Work quote"}
        window = self._window_stub(
            messages=[inbox_row],
            sidebar_account="acct-work",
            sidebar_folder="INBOX",
        )
        window._current_message = {
            "uid": "100",
            "subject": "Work quote",
            "_list_account_uid": "acct-work",
            "_list_folder": "INBOX",
        }

        self.assertTrue(MainWindow._reader_shows_list_key(window, "100"))

    def test_reader_shows_list_key_false_when_uid_collides_across_folders(self) -> None:
        from post.window import MainWindow

        inbox_row = {"uid": "55", "subject": "Inbox item"}
        window = self._window_stub(
            messages=[inbox_row],
            sidebar_account="acct-1",
            sidebar_folder="INBOX",
        )
        window._current_message = {
            "uid": "55",
            "subject": "Archived item",
            "_list_account_uid": "acct-1",
            "_list_folder": "Archive",
        }

        self.assertFalse(MainWindow._reader_shows_list_key(window, "55"))


class UniformBoolStateTests(unittest.TestCase):
    def test_empty_returns_none(self) -> None:
        self.assertIsNone(uniform_bool_state([]))

    def test_uniform_true(self) -> None:
        self.assertTrue(uniform_bool_state([True, True, True]))

    def test_uniform_false(self) -> None:
        self.assertFalse(uniform_bool_state([False, False]))

    def test_mixed_returns_none(self) -> None:
        self.assertIsNone(uniform_bool_state([True, False, True]))

    def test_single_value(self) -> None:
        self.assertTrue(uniform_bool_state([True]))
        self.assertFalse(uniform_bool_state([False]))


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
        self.assertEqual(format_recipient_header("a@example.com"), "a@example.com")

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
    def _base_info(self) -> MagicMock:
        info = MagicMock()
        info.get_uid.return_value = "1"
        info.get_subject.return_value = "Hi"
        info.get_from.return_value = "Alice <alice@example.com>"
        info.get_to.return_value = "Bob <bob@example.com>"
        info.get_cc.return_value = None
        info.get_date_sent.return_value = 1_700_000_000
        info.get_date_received.return_value = 1_700_000_100
        info.get_flags.return_value = 0
        info.get_size.return_value = 100
        info.get_message_id.return_value = 424242
        info.get_headers.return_value = None
        info.get_user_header.return_value = None
        info.get_user_tag.return_value = None
        return info

    def test_formats_camel_cc_address(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        cc = Camel.InternetAddress.new()
        cc.add("Carol", "carol@example.com")
        info = self._base_info()
        info.get_cc.return_value = cc
        result = message_info_to_dict(info)
        self.assertEqual(result["cc"], "Carol <carol@example.com>")

    def test_decodes_rfc2047_subject_bytes(self) -> None:
        info = self._base_info()
        info.get_subject.return_value = b"=?ISO-8859-1?Q?Gr=FC=DFe?="
        result = message_info_to_dict(info)
        self.assertEqual(result["subject"], "Grüße")

    def test_stores_rfc_message_id_not_camel_hash(self) -> None:
        info = self._base_info()
        headers = MagicMock()
        headers.get_length.return_value = 1
        headers.get_name.return_value = "Message-ID"
        headers.get_value.return_value = "<real-id@example.com>"
        info.get_headers.return_value = headers
        info.get_message_id.return_value = 999001
        result = message_info_to_dict(info)
        self.assertEqual(result["message_id"], "<real-id@example.com>")
        self.assertEqual(result["message_id_hash"], 999001)

    def test_hash_zero_omitted(self) -> None:
        info = self._base_info()
        info.get_message_id.return_value = 0
        result = message_info_to_dict(info)
        self.assertIsNone(result["message_id"])
        self.assertIsNone(result["message_id_hash"])

    def test_m365_flagged_from_follow_up_not_importance(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        info = self._base_info()
        # Importance High alone must not count as Flag on M365 (#270).
        info.get_flags.return_value = Camel.MessageFlags.FLAGGED
        info.get_user_tag.return_value = None
        result = message_info_to_dict(info, backend="microsoft365")
        self.assertFalse(result["flags"]["flagged"])

        info.get_user_tag.side_effect = lambda name: (
            "follow-up" if name == "follow-up" else None
        )
        result = message_info_to_dict(info, backend="microsoft365")
        self.assertTrue(result["flags"]["flagged"])

    def test_imap_flagged_from_flagged_bit(self) -> None:
        import gi

        gi.require_version("Camel", "1.2")
        from gi.repository import Camel

        info = self._base_info()
        info.get_flags.return_value = Camel.MessageFlags.FLAGGED
        result = message_info_to_dict(info, backend="imapx")
        self.assertTrue(result["flags"]["flagged"])


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
