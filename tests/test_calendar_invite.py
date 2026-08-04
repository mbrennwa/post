# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import email.message
import email.policy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from post.mail.calendar_invite import (
    build_vevent_ics,
    calendar_invite_from_email_message,
    find_meeting_url,
    find_meeting_url_in_html,
    format_invite_copy_text,
    format_invite_when,
    invite_join_url,
    invite_link_label,
    is_calendar_mime,
    merge_invite_details,
    parse_ics_invite,
)
from post.mail.calendar_write import (
    CalendarTarget,
    _source_is_writable,
    list_writable_calendars,
)
from post.mail.helpers import extract_attachments_from_email_message


_SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
DTEND:20260730T150000Z
LOCATION:Conference Room
ORGANIZER:MAILTO:organizer@example.com
DESCRIPTION:Weekly sync\\nJoin online
URL:https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc
END:VEVENT
END:VCALENDAR
"""


class CalendarMimeTests(unittest.TestCase):
    def test_is_calendar_mime(self) -> None:
        self.assertTrue(is_calendar_mime("text/calendar"))
        self.assertTrue(is_calendar_mime("text/calendar; method=REQUEST"))
        self.assertTrue(is_calendar_mime("application/ics"))
        self.assertTrue(is_calendar_mime("text/x-vcalendar"))
        self.assertFalse(is_calendar_mime("text/plain"))
        self.assertFalse(is_calendar_mime(None))


class ParseIcsInviteTests(unittest.TestCase):
    def test_parses_vevent_fields(self) -> None:
        invite = parse_ics_invite(_SAMPLE_ICS)
        assert invite is not None
        self.assertEqual(invite["title"], "Project sync")
        self.assertEqual(invite["start"], "2026-07-30T14:00:00+00:00")
        self.assertEqual(invite["end"], "2026-07-30T15:00:00+00:00")
        self.assertEqual(invite["location"], "Conference Room")
        self.assertEqual(invite["organizer"], "organizer@example.com")
        self.assertIn("teams.microsoft.com", invite["meeting_url"] or "")
        self.assertEqual(invite["method"], "REQUEST")
        self.assertEqual(invite["source"], "ics")

    def test_duration_without_dtend(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Short
DTSTART:20260101T100000Z
DURATION:PT30M
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        self.assertEqual(invite["end"], "2026-01-01T10:30:00+00:00")

    def test_teams_prop_url(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Teams
DTSTART:20260101T100000Z
X-MICROSOFT-SKYPETEAMSMEETINGURL:https://teams.microsoft.com/l/meetup-join/xyz
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        self.assertIn("teams.microsoft.com", invite["meeting_url"] or "")


class MeetingUrlTests(unittest.TestCase):
    def test_find_zoom_and_teams(self) -> None:
        self.assertIn(
            "zoom.us",
            find_meeting_url("Join https://us02web.zoom.us/j/123456 please") or "",
        )
        self.assertIn(
            "teams.microsoft.com",
            find_meeting_url(
                "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"
            )
            or "",
        )

    def test_html_href_preferred(self) -> None:
        html = (
            '<a href="https://zoom.us/j/999">Join Zoom</a> '
            "also https://example.com/not-a-meeting"
        )
        self.assertEqual(find_meeting_url_in_html(html), "https://zoom.us/j/999")

    def test_ignores_non_meeting_hosts(self) -> None:
        self.assertIsNone(find_meeting_url("See https://example.com/docs"))


class MergeInviteDetailsTests(unittest.TestCase):
    def test_meeting_link_only_has_no_guessed_times(self) -> None:
        invite = merge_invite_details(
            subject="Standup",
            body_plain="Join https://meet.google.com/abc-defg-hij",
        )
        assert invite is not None
        self.assertEqual(invite["title"], "Standup")
        self.assertIsNone(invite["start"])
        self.assertIsNone(invite["end"])
        self.assertEqual(invite["source"], "meeting_link")
        self.assertIn("meet.google.com", invite["meeting_url"] or "")

    def test_ics_plus_body_link(self) -> None:
        invite = merge_invite_details(
            subject="Fallback title",
            ics_text="""BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:
DTSTART:20260102T090000Z
END:VEVENT
END:VCALENDAR
""",
            body_html='<a href="https://zoom.us/j/1">Zoom</a>',
        )
        assert invite is not None
        self.assertEqual(invite["title"], "Fallback title")
        self.assertIn("zoom.us", invite["meeting_url"] or "")


class AttachmentExtractionTests(unittest.TestCase):
    def test_multipart_alternative_calendar_without_filename(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Meeting"
        msg.make_alternative()
        plain = email.message.EmailMessage()
        plain.set_content("See invite")
        calendar = email.message.EmailMessage()
        calendar.set_content(_SAMPLE_ICS, subtype="calendar")
        # Force text/calendar without disposition/filename.
        calendar.set_type("text/calendar")
        msg.attach(plain)
        msg.attach(calendar)

        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "invite.ics")
        self.assertTrue(is_calendar_mime(attachments[0]["mime_type"]))

        invite = calendar_invite_from_email_message(msg)
        assert invite is not None
        self.assertEqual(invite["title"], "Project sync")
        self.assertEqual(invite["attachment_index"], 0)

    def test_mixed_named_ics_still_listed(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Meeting"
        msg.set_content("Hello")
        msg.add_attachment(
            _SAMPLE_ICS.encode(),
            maintype="text",
            subtype="calendar",
            filename="invite.ics",
        )
        attachments = extract_attachments_from_email_message(msg)
        self.assertTrue(any(a["filename"] == "invite.ics" for a in attachments))

    def test_calendar_only_message(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Only calendar"
        msg.set_content(_SAMPLE_ICS, subtype="calendar")
        msg.set_type("text/calendar")
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        invite = calendar_invite_from_email_message(msg)
        assert invite is not None
        self.assertEqual(invite["title"], "Project sync")

    def test_plain_html_bodies_unchanged_by_calendar_presence(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Meeting"
        msg.make_alternative()
        plain = email.message.EmailMessage()
        plain.set_content("Plain body text")
        html = email.message.EmailMessage()
        html.set_content("<p>HTML body</p>", subtype="html")
        calendar = email.message.EmailMessage()
        calendar.set_content(_SAMPLE_ICS, subtype="calendar")
        calendar.set_type("text/calendar")
        msg.attach(plain)
        msg.attach(html)
        msg.attach(calendar)

        # Bodies come from email parts; invite must not swallow them.
        invite = calendar_invite_from_email_message(msg)
        assert invite is not None
        self.assertEqual(invite["title"], "Project sync")
        # Re-read bodies like the helper would.
        bodies = {"plain": None, "html": None}
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and bodies["plain"] is None:
                bodies["plain"] = part.get_content()
            elif ct == "text/html" and bodies["html"] is None:
                bodies["html"] = part.get_content()
        self.assertIn("Plain body", bodies["plain"] or "")
        self.assertIn("HTML body", bodies["html"] or "")


class InviteJoinUrlTests(unittest.TestCase):
    def test_prefers_meeting_url(self) -> None:
        self.assertEqual(
            invite_join_url(
                {
                    "meeting_url": "https://zoom.us/j/1",
                    "location": "Room A",
                }
            ),
            "https://zoom.us/j/1",
        )

    def test_uses_http_location(self) -> None:
        self.assertEqual(
            invite_join_url({"location": "https://example.com/join"}),
            "https://example.com/join",
        )

    def test_finds_teams_in_description(self) -> None:
        self.assertIn(
            "teams.microsoft.com",
            invite_join_url(
                {
                    "description": "Join https://teams.microsoft.com/l/meetup-join/abc",
                }
            )
            or "",
        )


class FormatInviteCopyTextTests(unittest.TestCase):
    def test_omits_description_body(self) -> None:
        text = format_invite_copy_text(
            {
                "title": "Standup",
                "start": "2026-08-12T10:20:00",
                "end": "2026-08-12T12:45:00",
                "location": "Microsoft Teams Meeting",
                "organizer": "a@example.com",
                "meeting_url": "https://teams.microsoft.com/l/meetup-join/abc",
                "description": "Dear all\n\nWe will meet...\n",
            }
        )
        self.assertIn("Standup", text)
        self.assertIn("When:", text)
        self.assertIn("Link: https://teams.microsoft.com/", text)
        self.assertNotIn("Dear all", text)
        self.assertNotIn("We will meet", text)

    def test_invite_link_label_uses_host(self) -> None:
        self.assertEqual(
            invite_link_label(
                "https://teams.microsoft.com/l/meetup-join/abc?x=1"
            ),
            "Link: teams.microsoft.com",
        )


class FormatInviteWhenTests(unittest.TestCase):
    def test_same_day_replaces_t_and_omits_repeated_date(self) -> None:
        text = format_invite_when(
            {
                "start": "2026-07-30T14:00:00+00:00",
                "end": "2026-07-30T15:00:00+00:00",
            }
        )
        self.assertEqual(text, "2026-07-30 14:00 – 15:00")

    def test_shows_nonzero_seconds(self) -> None:
        text = format_invite_when(
            {
                "start": "2026-07-30T14:00:30",
                "end": "2026-07-30T15:00:05",
            }
        )
        self.assertEqual(text, "2026-07-30 14:00:30 – 15:00:05")

    def test_different_days_keep_both_dates(self) -> None:
        text = format_invite_when(
            {
                "start": "2026-07-30T22:00:00",
                "end": "2026-07-31T01:00:00",
            }
        )
        self.assertEqual(text, "2026-07-30 22:00 – 2026-07-31 01:00")

    def test_all_day(self) -> None:
        self.assertEqual(
            format_invite_when({"start": "2026-07-30", "all_day": True}),
            "2026-07-30 (all day)",
        )


class BuildVeventTests(unittest.TestCase):
    def test_requires_start(self) -> None:
        with self.assertRaises(ValueError):
            build_vevent_ics({"title": "X", "meeting_url": "https://zoom.us/j/1"})

    def test_builds_minimal_event(self) -> None:
        ics = build_vevent_ics(
            {
                "title": "Sync",
                "start": "2026-07-30T14:00:00",
                "end": "2026-07-30T15:00:00",
                "meeting_url": "https://zoom.us/j/1",
                "uid": "test-uid-123",
            }
        )
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertNotIn("BEGIN:VCALENDAR", ics)
        self.assertIn("UID:test-uid-123", ics)
        self.assertIn("DTSTAMP:", ics)
        self.assertIn("SUMMARY:Sync", ics)
        self.assertIn("LOCATION:https://zoom.us/j/1", ics)
        self.assertIn("URL:https://zoom.us/j/1", ics)

    def test_teams_label_location_uses_meeting_url(self) -> None:
        ics = build_vevent_ics(
            {
                "title": "Discussion",
                "start": "2026-08-07T10:00:00",
                "end": "2026-08-07T11:30:00",
                "location": "Microsoft Teams Meeting",
                "meeting_url": "https://teams.microsoft.com/l/meetup-join/abc",
                "description": "Agenda item one",
                "uid": "teams-uid-1",
            }
        )
        self.assertIn(
            "LOCATION:https://teams.microsoft.com/l/meetup-join/abc", ics
        )
        self.assertIn("URL:https://teams.microsoft.com/l/meetup-join/abc", ics)
        self.assertNotIn("LOCATION:Microsoft Teams Meeting", ics)
        self.assertIn("Microsoft Teams Meeting", ics)
        self.assertIn("Agenda item one", ics)

    def test_room_only_location_without_meeting_url(self) -> None:
        ics = build_vevent_ics(
            {
                "title": "In person",
                "start": "2026-08-07T10:00:00",
                "location": "Conference Room B",
                "uid": "room-uid-1",
            }
        )
        self.assertIn("LOCATION:Conference Room B", ics)
        self.assertNotIn("URL:", ics)


class WritableCalendarFilterTests(unittest.TestCase):
    def test_skips_birthday_and_holiday_names(self) -> None:
        birthday = SimpleNamespace(
            get_writable=lambda: True,
            get_display_name=lambda: "Birthdays",
            get_extension=lambda _name: None,
            has_extension=lambda _name: False,
        )
        holiday = SimpleNamespace(
            get_writable=lambda: True,
            get_display_name=lambda: "Feiertage in der Schweiz",
            get_extension=lambda _name: None,
            has_extension=lambda _name: False,
        )
        work = SimpleNamespace(
            get_writable=lambda: True,
            get_display_name=lambda: "Work",
            get_extension=lambda _name: None,
            has_extension=lambda _name: False,
        )
        self.assertFalse(_source_is_writable(birthday))
        self.assertFalse(_source_is_writable(holiday))
        self.assertTrue(_source_is_writable(work))

    def test_list_writable_calendars_filters(self) -> None:
        work = MagicMock()
        work.get_enabled.return_value = True
        work.get_writable.return_value = True
        work.get_display_name.return_value = "Work"
        work.get_uid.return_value = "work-uid"
        work.get_parent.return_value = None
        work.get_extension.return_value = None
        work.has_extension.return_value = False

        birthdays = MagicMock()
        birthdays.get_enabled.return_value = True
        birthdays.get_writable.return_value = True
        birthdays.get_display_name.return_value = "Birthdays"
        birthdays.get_uid.return_value = "bday-uid"
        birthdays.get_parent.return_value = None
        birthdays.get_extension.return_value = None
        birthdays.has_extension.return_value = False

        registry = MagicMock()
        registry.list_sources.return_value = [work, birthdays]

        fake_eds = MagicMock()
        fake_eds.SOURCE_EXTENSION_CALENDAR = "Calendar"

        with patch("gi.require_version"), patch(
            "gi.repository.EDataServer", fake_eds, create=True
        ):
            targets = list_writable_calendars(registry=registry)

        self.assertEqual(
            targets,
            [CalendarTarget(uid="work-uid", display_name="Work", parent_name=None)],
        )


if __name__ == "__main__":
    unittest.main()
