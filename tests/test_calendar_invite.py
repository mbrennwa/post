# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import email.message
import email.policy
import os
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from post.mail.calendar_invite import (
    build_vevent_ics,
    calendar_invite_from_email_message,
    canonical_meeting_url,
    find_meeting_url,
    find_meeting_url_in_html,
    format_invite_copy_text,
    format_invite_when,
    invite_join_url,
    invite_link_label,
    is_calendar_mime,
    is_join_url,
    looks_like_calendar_attachment,
    markup_safe_notes,
    merge_invite_details,
    parse_ics_invite,
    strip_join_urls,
)
from post.mail.calendar_write import (
    CalendarTarget,
    _source_is_selected,
    _source_is_writable,
    list_writable_calendars,
)
from post.mail.helpers import extract_attachments_from_email_message


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


def _ics_description(ics: str) -> str:
    """Return the unescaped DESCRIPTION text of a built VEVENT ('' if absent)."""
    for line in ics.split("\r\n"):
        if line.startswith("DESCRIPTION:"):
            raw = line[len("DESCRIPTION:") :]
            return (
                raw.replace("\\n", "\n")
                .replace("\\,", ",")
                .replace("\\;", ";")
                .replace("\\\\", "\\")
            )
    return ""


def _ics_prop(ics: str, name: str) -> str | None:
    """Return the raw value of the first *name* property line, or None."""
    for line in ics.split("\r\n"):
        if line.startswith(f"{name}:"):
            return line[len(name) + 1 :]
    return None


def _notes_before_block(notes: str) -> str:
    """Notes text before the first Teams delimiter line.

    GNOME Calendar renders only this part as markup; the block after it is only
    scanned for the Join URL.
    """
    return notes.split(_TEAMS_DELIMITER, 1)[0]


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

_TEAMS_MEET_URL = "https://teams.microsoft.com/meet/390576036169085?p=PassCode1"
_TEAMS_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"
)
_TEAMS_DELIMITER = "_" * 80
_TEAMS_DUAL_ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
DTEND:20260730T150000Z
LOCATION:Microsoft Teams Meeting
URL:{_TEAMS_JOIN_URL}
X-MICROSOFT-SKYPETEAMSMEETINGURL:{_TEAMS_JOIN_URL}
DESCRIPTION:{_TEAMS_DELIMITER}\\nMicrosoft Teams meeting\\nJoin: {_TEAMS_MEET_URL}\\nMeeting ID: 390 576 036 169 085\\nPasscode: PassCode1\\nNeed help?<https://aka.ms/JoinTeamsMeeting> | System reference<{_TEAMS_JOIN_URL}>\\nDial in by phone\\nhttps://dialin.teams.cloud.microsoft/example\\nFor organizers: https://teams.microsoft.com/meetingOptions/?organizerId=abc\\n{_TEAMS_DELIMITER}
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

    def test_looks_like_calendar_attachment(self) -> None:
        self.assertTrue(looks_like_calendar_attachment("text/calendar", None))
        self.assertTrue(
            looks_like_calendar_attachment("application/octet-stream", "reservation.ics")
        )
        self.assertTrue(looks_like_calendar_attachment("application/octet-stream", "Invite.VCS"))
        self.assertFalse(
            looks_like_calendar_attachment("application/octet-stream", "notes.txt")
        )
        self.assertFalse(looks_like_calendar_attachment("application/octet-stream", None))
        self.assertFalse(looks_like_calendar_attachment(None, None))


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
        self.assertIsNone(invite.get("tzid"))

    def test_tzid_london_converts_to_utc_summer(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:London meet
DTSTART;TZID=Europe/London:20260730T100000
DTEND;TZID=Europe/London:20260730T110000
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        # BST (UTC+1) → 09:00Z / 10:00Z
        self.assertEqual(invite["start"], "2026-07-30T09:00:00+00:00")
        self.assertEqual(invite["end"], "2026-07-30T10:00:00+00:00")
        self.assertEqual(invite["tzid"], "Europe/London")

    def test_tzid_london_converts_to_utc_winter(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:London winter
DTSTART;TZID=Europe/London:20260115T100000
DTEND;TZID=Europe/London:20260115T110000
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        # GMT (UTC+0)
        self.assertEqual(invite["start"], "2026-01-15T10:00:00+00:00")
        self.assertEqual(invite["end"], "2026-01-15T11:00:00+00:00")
        self.assertEqual(invite["tzid"], "Europe/London")

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

    def test_prefers_meet_join_over_ics_url(self) -> None:
        invite = parse_ics_invite(_TEAMS_DUAL_ICS)
        assert invite is not None
        self.assertEqual(invite["meeting_url"], _TEAMS_MEET_URL)

    def test_skype_prop_plus_description_meet_url(self) -> None:
        ics = f"""BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Teams
DTSTART:20260101T100000Z
X-MICROSOFT-SKYPETEAMSMEETINGURL:{_TEAMS_JOIN_URL}
DESCRIPTION:Join {_TEAMS_MEET_URL}
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        self.assertEqual(invite["meeting_url"], _TEAMS_MEET_URL)


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

    def test_canonical_prefers_teams_meet(self) -> None:
        self.assertEqual(
            canonical_meeting_url(
                _TEAMS_JOIN_URL,
                f"Join {_TEAMS_MEET_URL}",
                fallback=_TEAMS_JOIN_URL,
            ),
            _TEAMS_MEET_URL,
        )

    def test_canonical_falls_back_to_meetup_join(self) -> None:
        self.assertEqual(
            canonical_meeting_url(
                "No meeting here",
                fallback=_TEAMS_JOIN_URL,
            ),
            _TEAMS_JOIN_URL,
        )

    def test_strip_join_urls_keeps_help_options_dialin(self) -> None:
        notes = (
            f"{'_' * 80}\n"
            f"Join: {_TEAMS_MEET_URL}\n"
            "Meeting ID: 390 576 036 169 085\n"
            "Passcode: PassCode1\n"
            f"Need help?<https://aka.ms/JoinTeamsMeeting> | "
            f"System reference<{_TEAMS_JOIN_URL}>\n"
            "https://dialin.teams.cloud.microsoft/example\n"
            "https://teams.microsoft.com/meetingOptions/?organizerId=abc\n"
            f"{'_' * 80}\n"
        )
        stripped = strip_join_urls(notes)
        self.assertNotIn("/meet/", stripped)
        self.assertNotIn("meetup-join", stripped)
        self.assertNotIn("____", stripped)
        self.assertNotIn("Join:", stripped)
        self.assertNotIn("System reference", stripped)
        self.assertIn("Meeting ID: 390 576 036 169 085", stripped)
        self.assertIn("Passcode: PassCode1", stripped)
        self.assertIn("https://aka.ms/JoinTeamsMeeting", stripped)
        self.assertIn("https://dialin.teams.cloud.microsoft/example", stripped)
        self.assertIn(
            "https://teams.microsoft.com/meetingOptions/?organizerId=abc",
            stripped,
        )
        self.assertFalse(is_join_url("https://teams.microsoft.com/meetingOptions/?x=1"))
        self.assertTrue(is_join_url(_TEAMS_MEET_URL))
        self.assertTrue(is_join_url(_TEAMS_JOIN_URL))


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

    def test_quoted_outlook_teams_link_ignored_on_reply(self) -> None:
        html = (
            "<div>Hello, the samples are on the way.</div>"
            '<div id="appendonsend"></div><hr>'
            '<div id="divRplyFwdMsg">'
            "Microsoft Teams meeting "
            '<a href="https://teams.microsoft.com/meet/219697379367806">Join</a>'
            "</div>"
        )
        invite = merge_invite_details(
            subject="Re: project sync",
            body_html=html,
            body_plain=(
                "Hello, the samples are on the way.\n\n"
                "On Tue, Alice wrote:\n"
                "> Join https://teams.microsoft.com/meet/219697379367806\n"
            ),
        )
        self.assertIsNone(invite)

    def test_forward_subject_keeps_quoted_meeting_link(self) -> None:
        html = (
            "<div>FYI</div>"
            '<div id="appendonsend"></div><hr>'
            '<div id="divRplyFwdMsg">'
            '<a href="https://teams.microsoft.com/meet/219697379367806">Join</a>'
            "</div>"
        )
        invite = merge_invite_details(subject="Fwd: project sync", body_html=html)
        assert invite is not None
        self.assertEqual(invite["source"], "meeting_link")
        self.assertIn("teams.microsoft.com", invite["meeting_url"] or "")

    def test_new_meeting_link_above_quote_still_detected(self) -> None:
        html = (
            '<div>Join <a href="https://meet.google.com/abc-defg-hij">Meet</a></div>'
            '<div id="appendonsend"></div><hr>'
            "<div>old thread</div>"
        )
        invite = merge_invite_details(subject="Re: standup", body_html=html)
        assert invite is not None
        self.assertEqual(invite["source"], "meeting_link")
        self.assertIn("meet.google.com", invite["meeting_url"] or "")

    def test_plain_reply_ignores_quoted_meeting_link(self) -> None:
        invite = merge_invite_details(
            subject="Re: sync",
            body_plain=(
                "Thanks, that works.\n\n"
                "On Mon, Alice wrote:\n"
                "> Join https://zoom.us/j/999\n"
            ),
        )
        self.assertIsNone(invite)

    def test_ics_meetup_join_uses_body_meet_url(self) -> None:
        invite = merge_invite_details(
            subject="Project sync",
            ics_text=f"""BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
DTEND:20260730T150000Z
URL:{_TEAMS_JOIN_URL}
END:VEVENT
END:VCALENDAR
""",
            body_html=f'<a href="{_TEAMS_MEET_URL}">Join</a>',
        )
        assert invite is not None
        self.assertEqual(invite["meeting_url"], _TEAMS_MEET_URL)
        self.assertEqual(invite_join_url(invite), _TEAMS_MEET_URL)

    def test_ics_reply_method_is_not_an_invite(self) -> None:
        ics = """BEGIN:VCALENDAR
METHOD:REPLY
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
DTEND:20260730T150000Z
URL:https://teams.microsoft.com/l/meetup-join/abc
END:VEVENT
END:VCALENDAR
"""
        self.assertIsNone(
            merge_invite_details(
                subject="Accepted: Project sync",
                ics_text=ics,
                body_plain="Join https://teams.microsoft.com/l/meetup-join/abc",
            )
        )

    def test_ics_cancel_method_is_not_an_invite(self) -> None:
        ics = """BEGIN:VCALENDAR
METHOD:CANCEL
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
END:VEVENT
END:VCALENDAR
"""
        self.assertIsNone(merge_invite_details(subject="Canceled: Project sync", ics_text=ics))

    def test_ics_counter_method_is_not_an_invite(self) -> None:
        ics = """BEGIN:VCALENDAR
METHOD:COUNTER
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T160000Z
END:VEVENT
END:VCALENDAR
"""
        self.assertIsNone(merge_invite_details(subject="New time proposed", ics_text=ics))

    def test_ics_publish_is_addable(self) -> None:
        ics = """BEGIN:VCALENDAR
METHOD:PUBLISH
BEGIN:VEVENT
SUMMARY:Holiday
DTSTART:20261224T000000Z
END:VEVENT
END:VCALENDAR
"""
        invite = merge_invite_details(subject="Holiday", ics_text=ics)
        assert invite is not None
        self.assertEqual(invite["source"], "ics")
        self.assertEqual((invite["method"] or "").upper(), "PUBLISH")
        self.assertEqual(invite["title"], "Holiday")

    def test_ics_missing_method_is_addable(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
END:VEVENT
END:VCALENDAR
"""
        invite = merge_invite_details(subject="Project sync", ics_text=ics)
        assert invite is not None
        self.assertEqual(invite["source"], "ics")
        self.assertIsNone(invite["method"])


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

    def test_octet_stream_ics_filename_is_invite(self) -> None:
        """Mislabeled .ics (e.g. booking confirmations) must still be detected (#322)."""
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Reservation Confirmation"
        msg.set_content("Your booking is confirmed.")
        msg.add_attachment(
            _SAMPLE_ICS.encode(),
            maintype="application",
            subtype="octet-stream",
            filename="reservation-745204C7.ics",
        )
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "reservation-745204C7.ics")
        self.assertFalse(is_calendar_mime(attachments[0]["mime_type"]))
        self.assertTrue(
            looks_like_calendar_attachment(
                attachments[0]["mime_type"], attachments[0]["filename"]
            )
        )

        invite = calendar_invite_from_email_message(msg)
        assert invite is not None
        self.assertEqual(invite["title"], "Project sync")
        self.assertEqual(invite["attachment_index"], 0)
        self.assertEqual(invite["source"], "ics")

    def test_octet_stream_ics_reply_method_not_invite(self) -> None:
        ics = """BEGIN:VCALENDAR
METHOD:REPLY
BEGIN:VEVENT
SUMMARY:Project sync
DTSTART:20260730T140000Z
END:VEVENT
END:VCALENDAR
"""
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Accepted: Project sync"
        msg.set_content("Accepted")
        msg.add_attachment(
            ics.encode(),
            maintype="application",
            subtype="octet-stream",
            filename="reply.ics",
        )
        self.assertIsNone(calendar_invite_from_email_message(msg))

    def test_octet_stream_without_ics_filename_ignored(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Notes"
        msg.set_content("See file")
        msg.add_attachment(
            _SAMPLE_ICS.encode(),
            maintype="application",
            subtype="octet-stream",
            filename="notes.bin",
        )
        self.assertIsNone(calendar_invite_from_email_message(msg))

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

    def test_cid_inline_image_excluded_from_attachments(self) -> None:
        """CID/inline images shown in the body must not appear as attachments (#258)."""
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
            b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Signed"
        msg.make_mixed()

        related = email.message.EmailMessage()
        related.make_related()
        html = email.message.EmailMessage()
        html.set_content(
            '<p>Hi</p><img src="cid:logo@local">',
            subtype="html",
        )
        related.attach(html)
        logo = email.message.EmailMessage()
        logo.set_content(
            png,
            maintype="image",
            subtype="png",
            disposition="inline",
            filename="logo.png",
        )
        logo["Content-ID"] = "<logo@local>"
        related.attach(logo)
        msg.attach(related)

        msg.add_attachment(
            b"hello notes",
            maintype="text",
            subtype="plain",
            filename="notes.txt",
        )

        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "notes.txt")
        self.assertEqual(attachments[0]["index"], 0)
        self.assertFalse(any("logo" in (a["filename"] or "") for a in attachments))

    def test_inline_disposition_image_without_cid_excluded(self) -> None:
        body = email.message.EmailMessage()
        body.set_content("Hello")
        img = email.message.EmailMessage()
        img.set_content(
            b"fakepng",
            maintype="image",
            subtype="png",
            disposition="inline",
            filename="badge.png",
        )

        mixed = email.message.EmailMessage(policy=email.policy.default)
        mixed["Subject"] = "Decor"
        mixed.make_mixed()
        mixed.attach(body)
        mixed.attach(img)
        mixed.add_attachment(
            b"real",
            maintype="application",
            subtype="octet-stream",
            filename="file.bin",
        )

        attachments = extract_attachments_from_email_message(mixed)
        names = [a["filename"] for a in attachments]
        self.assertNotIn("badge.png", names)
        self.assertIn("file.bin", names)

    def test_explicit_attachment_image_with_cid_still_listed(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "Photo"
        msg.set_content("See attached")
        msg.add_attachment(
            b"fakepng",
            maintype="image",
            subtype="png",
            filename="photo.png",
        )
        # Attachments added via add_attachment get disposition=attachment.
        # Also set Content-ID as some clients do for downloadable images.
        for part in msg.walk():
            if part.get_filename() == "photo.png":
                part["Content-ID"] = "<photo@local>"
                break

        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "photo.png")
        self.assertEqual(attachments[0]["mime_type"], "image/png")


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
    def test_utc_shows_zurich_local_summer_with_zone_label(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(
                {
                    "start": "2026-07-30T14:00:00+00:00",
                    "end": "2026-07-30T15:00:00+00:00",
                }
            )
        self.assertEqual(text, "2026-07-30 16:00 – 17:00 CEST (Europe/Zurich)")

    def test_utc_shows_zurich_local_winter_with_zone_label(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(
                {
                    "start": "2026-01-15T14:00:00+00:00",
                    "end": "2026-01-15T15:00:00+00:00",
                }
            )
        self.assertEqual(text, "2026-01-15 15:00 – 16:00 CET (Europe/Zurich)")

    def test_london_tzid_invite_displays_in_zurich_summer(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:London meet
DTSTART;TZID=Europe/London:20260730T100000
DTEND;TZID=Europe/London:20260730T110000
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(invite)
        # 10:00 BST = 09:00Z = 11:00 CEST
        self.assertEqual(text, "2026-07-30 11:00 – 12:00 CEST (Europe/Zurich)")

    def test_london_tzid_invite_displays_in_zurich_winter(self) -> None:
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:London winter
DTSTART;TZID=Europe/London:20260115T100000
DTEND;TZID=Europe/London:20260115T110000
END:VEVENT
END:VCALENDAR
"""
        invite = parse_ics_invite(ics)
        assert invite is not None
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(invite)
        # 10:00 GMT = 10:00Z = 11:00 CET
        self.assertEqual(text, "2026-01-15 11:00 – 12:00 CET (Europe/Zurich)")

    def test_shows_nonzero_seconds(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(
                {
                    "start": "2026-07-30T14:00:30",
                    "end": "2026-07-30T15:00:05",
                }
            )
        self.assertEqual(
            text, "2026-07-30 14:00:30 – 15:00:05 CEST (Europe/Zurich)"
        )

    def test_different_days_keep_both_dates(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            text = format_invite_when(
                {
                    "start": "2026-07-30T22:00:00",
                    "end": "2026-07-31T01:00:00",
                }
            )
        self.assertEqual(
            text,
            "2026-07-30 22:00 – 2026-07-31 01:00 CEST (Europe/Zurich)",
        )

    def test_all_day(self) -> None:
        self.assertEqual(
            format_invite_when({"start": "2026-07-30", "all_day": True}),
            "2026-07-30 (all day)",
        )


class BuildVeventTests(unittest.TestCase):
    def test_requires_start(self) -> None:
        with self.assertRaises(ValueError):
            build_vevent_ics({"title": "X", "meeting_url": "https://zoom.us/j/1"})

    def test_writes_utc_z_from_offset_iso(self) -> None:
        ics = build_vevent_ics(
            {
                "title": "Sync",
                "start": "2026-07-30T14:00:00+00:00",
                "end": "2026-07-30T15:00:00+00:00",
                "uid": "utc-uid-1",
            }
        )
        self.assertIn("DTSTART:20260730T140000Z", ics)
        self.assertIn("DTEND:20260730T150000Z", ics)

    def test_naive_local_converts_to_utc_z(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Local",
                    "start": "2026-07-30T16:00:00",
                    "end": "2026-07-30T17:00:00",
                    "uid": "local-uid-1",
                }
            )
        # 16:00 CEST = 14:00Z
        self.assertIn("DTSTART:20260730T140000Z", ics)
        self.assertIn("DTEND:20260730T150000Z", ics)

    def test_builds_minimal_event(self) -> None:
        with fixed_timezone("Europe/Zurich"):
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
        self.assertIn("URL:https://zoom.us/j/1", ics)
        self.assertIn("DTSTART:20260730T120000Z", ics)
        # Links never go into LOCATION (#534): Google Calendar strips them.
        self.assertIsNone(_ics_prop(ics, "LOCATION"))
        self.assertEqual(_ics_description(ics), "Link: https://zoom.us/j/1")

    def test_teams_label_stays_location_and_link_goes_to_notes(self) -> None:
        with fixed_timezone("Europe/Zurich"):
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
        # The organiser's own non-URL location text is kept as LOCATION.
        self.assertEqual(_ics_prop(ics, "LOCATION"), "Microsoft Teams Meeting")
        self.assertIn("URL:https://teams.microsoft.com/l/meetup-join/abc", ics)
        self.assertEqual(
            _ics_description(ics),
            "Agenda item one\n"
            f"{_TEAMS_DELIMITER}\n"
            "Join: https://teams.microsoft.com/l/meetup-join/abc\n"
            f"{_TEAMS_DELIMITER}",
        )

    def test_room_and_meeting_url_puts_room_in_location(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Hybrid",
                    "start": "2026-08-07T10:00:00",
                    "location": "Conference Room B",
                    "meeting_url": "https://zoom.us/j/1",
                    "uid": "hybrid-uid-1",
                }
            )
        # #534: the room is the place; the link is in the notes (and URL).
        self.assertEqual(_ics_prop(ics, "LOCATION"), "Conference Room B")
        self.assertIn("URL:https://zoom.us/j/1", ics)
        self.assertEqual(_ics_description(ics), "Link: https://zoom.us/j/1")

    def test_location_never_holds_a_join_url(self) -> None:
        cases = [
            {"meeting_url": "https://zoom.us/j/1"},
            {"meeting_url": _TEAMS_MEET_URL, "location": "Microsoft Teams Meeting"},
            {"meeting_url": _TEAMS_MEET_URL, "location": _TEAMS_MEET_URL},
            {"meeting_url": "https://meet.google.com/abc-defg-hij"},
            {"meeting_url": "https://zoom.us/j/1", "location": "https://zoom.us/j/1"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                with fixed_timezone("Europe/Zurich"):
                    ics = build_vevent_ics(
                        {
                            "title": "X",
                            "start": "2026-08-07T10:00:00",
                            "uid": "nj-1",
                            **extra,
                        }
                    )
                location = _ics_prop(ics, "LOCATION") or ""
                self.assertNotIn("http", location)
                self.assertIn(extra["meeting_url"], _ics_description(ics))

    def test_dual_teams_urls_write_single_meet_join(self) -> None:
        invite = parse_ics_invite(_TEAMS_DUAL_ICS)
        assert invite is not None
        invite["uid"] = "dual-teams-1"
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(invite)
        self.assertEqual(_ics_prop(ics, "LOCATION"), "Microsoft Teams Meeting")
        self.assertIn(f"URL:{_TEAMS_MEET_URL}", ics)
        description = _ics_description(ics)
        # Exactly one join URL in the notes, inside one delimited block (#382).
        self.assertEqual(description.count("teams.microsoft.com/meet/"), 1)
        self.assertEqual(description.count(_TEAMS_DELIMITER), 2)
        self.assertTrue(
            description.endswith(
                f"{_TEAMS_DELIMITER}\nJoin: {_TEAMS_MEET_URL}\n{_TEAMS_DELIMITER}"
            )
        )
        before_block = _notes_before_block(description)
        self.assertIn("Meeting ID: 390 576 036 169 085", before_block)
        self.assertIn("Passcode: PassCode1", before_block)
        self.assertIn("aka.ms/JoinTeamsMeeting", before_block)
        self.assertIn("dialin.teams.cloud.microsoft", before_block)
        self.assertIn("meetingOptions", before_block)
        self.assertNotIn("meetup-join", description)
        self.assertNotIn("System reference", description)
        self.assertNotIn("<", description)

    def test_meetup_join_only_not_copied_into_notes(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Legacy Teams",
                    "start": "2026-08-07T10:00:00",
                    "meeting_url": _TEAMS_JOIN_URL,
                    "description": f"System reference<{_TEAMS_JOIN_URL}>",
                    "uid": "legacy-teams-1",
                }
            )
        self.assertIsNone(_ics_prop(ics, "LOCATION"))
        # The body's "System reference<url>" is stripped; the block carries the
        # single join URL.
        self.assertEqual(
            _ics_description(ics),
            f"{_TEAMS_DELIMITER}\nJoin: {_TEAMS_JOIN_URL}\n{_TEAMS_DELIMITER}",
        )

    def test_room_only_location_without_meeting_url(self) -> None:
        with fixed_timezone("Europe/Zurich"):
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
        self.assertNotIn("DESCRIPTION:", ics)

    def test_hybrid_teams_notes_are_valid_markup_with_room(self) -> None:
        # #534: GNOME Calendar renders notes as markup; Teams "Need help?<url>"
        # made them invalid, which blanked the notes.
        invite = parse_ics_invite(_TEAMS_DUAL_ICS)
        assert invite is not None
        invite["location"] = "Conference Room B"
        invite["uid"] = "hybrid-teams-1"
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(invite)
        notes = _ics_description(ics)
        before_block = _notes_before_block(notes)
        self.assertEqual(_ics_prop(ics, "LOCATION"), "Conference Room B")
        self.assertIn("Meeting ID: 390 576 036 169 085", before_block)
        self.assertIn("Passcode: PassCode1", before_block)
        self.assertIn("Need help? https://aka.ms/JoinTeamsMeeting", before_block)
        self.assertIn("https://dialin.teams.cloud.microsoft/example", before_block)
        self.assertNotIn("<", before_block)
        self.assertNotIn(">", before_block)
        self.assertEqual(notes.count("/meet/"), 1)
        self.assertMarkupValid(before_block)
        self.assertIn(f"URL:{_TEAMS_MEET_URL}", ics)

    def test_ampersand_in_notes_and_room_is_escaped(self) -> None:
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Hybrid",
                    "start": "2026-08-07T10:00:00",
                    "location": "R&D Room",
                    "meeting_url": "https://zoom.us/j/1",
                    "description": (
                        "Options: https://example.com/opts?a=1&b=2 "
                        "(Tom & Jerry)"
                    ),
                    "uid": "amp-uid-1",
                }
            )
        notes = _ics_description(ics)
        # The room is LOCATION, rendered as markup by GNOME Calendar too.
        # (ICS text escaping turns the entity's ";" into "\;"; parsers undo it.)
        self.assertIn("LOCATION:R&amp\\;D Room", ics)
        self.assertIn("https://example.com/opts?a=1&amp;b=2", notes)
        self.assertIn("Tom &amp; Jerry", notes)
        self.assertMarkupValid(_notes_before_block(notes))

    def test_link_url_with_ampersand_stays_raw_in_url_property(self) -> None:
        url = "https://example.webex.com/meet/room?pwd=abc&mtid=123"
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Webex",
                    "start": "2026-08-07T10:00:00",
                    "location": "Webex Meeting",
                    "meeting_url": url,
                    "description": f"Join: {url}\nAgenda & notes",
                    "uid": "webex-amp-1",
                }
            )
        self.assertIn(f"URL:{url}", ics)
        self.assertEqual(_ics_prop(ics, "LOCATION"), "Webex Meeting")
        notes = _ics_description(ics)
        # The visible link text is escaped for the markup label, once.
        self.assertEqual(notes.count("mtid"), 1)
        self.assertIn(
            "Link: https://example.webex.com/meet/room?pwd=abc&amp;mtid=123", notes
        )
        self.assertIn("Agenda &amp; notes", notes)
        self.assertMarkupValid(_notes_before_block(notes))

    def test_teams_block_url_is_not_markup_escaped(self) -> None:
        # GNOME Calendar reads the Join URL from the raw notes text between the
        # delimiter lines; "&amp;" there would break the Join button.
        url = "https://teams.microsoft.com/meet/1?p=a&b=2"
        with fixed_timezone("Europe/Zurich"):
            ics = build_vevent_ics(
                {
                    "title": "Teams",
                    "start": "2026-08-07T10:00:00",
                    "meeting_url": url,
                    "description": "Agenda & more",
                    "uid": "teams-amp-1",
                }
            )
        notes = _ics_description(ics)
        self.assertTrue(
            notes.endswith(f"{_TEAMS_DELIMITER}\nJoin: {url}\n{_TEAMS_DELIMITER}")
        )
        self.assertIn("Agenda &amp; more", notes)
        self.assertIn(f"URL:{url}", ics)

    def assertMarkupValid(self, notes: str) -> None:
        # Pure-Python stand-in for Pango markup parsing (no GTK in tests).
        try:
            ElementTree.fromstring(f"<notes>{notes}</notes>")
        except ElementTree.ParseError as exc:  # pragma: no cover - failure path
            self.fail(f"notes are not valid markup: {exc}\n{notes!r}")


class MarkupSafeNotesTests(unittest.TestCase):
    def test_unwraps_angle_bracketed_urls(self) -> None:
        self.assertEqual(
            markup_safe_notes("Need help?<https://aka.ms/JoinTeamsMeeting> ok"),
            "Need help? https://aka.ms/JoinTeamsMeeting ok",
        )

    def test_unwrapped_url_is_separated_from_preceding_label(self) -> None:
        self.assertEqual(
            markup_safe_notes("Meeting options<https://x.example/o?a=1>"),
            "Meeting options https://x.example/o?a=1".replace("&", "&amp;"),
        )
        # Already separated, or at the start of the text/line: no extra space.
        self.assertEqual(markup_safe_notes("<https://x.example/a>"), "https://x.example/a")
        self.assertEqual(
            markup_safe_notes("a\n<https://x.example/a>"), "a\nhttps://x.example/a"
        )
        self.assertEqual(
            markup_safe_notes("Help <https://x.example/a>"), "Help https://x.example/a"
        )

    def test_unwraps_addresses_and_drops_stray_brackets(self) -> None:
        self.assertEqual(
            markup_safe_notes("Tom <tom@example.com>"), "Tom tom@example.com"
        )
        self.assertEqual(markup_safe_notes("a < b > c <"), "a  b  c ")

    def test_escapes_ampersand(self) -> None:
        self.assertEqual(markup_safe_notes("R&D & more"), "R&amp;D &amp; more")

    def test_plain_text_unchanged_and_empty(self) -> None:
        self.assertEqual(markup_safe_notes("Plain\ntext: 1, 2; 3"), "Plain\ntext: 1, 2; 3")
        self.assertEqual(markup_safe_notes(""), "")
        self.assertEqual(markup_safe_notes(None), "")


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

    def test_source_is_selected_uses_calendar_checkbox(self) -> None:
        checked = MagicMock()
        checked.get_extension.return_value.get_selected.return_value = True
        unchecked = MagicMock()
        unchecked.get_extension.return_value.get_selected.return_value = False
        unknown = SimpleNamespace(get_extension=lambda _name: None)
        self.assertTrue(_source_is_selected(checked))
        self.assertFalse(_source_is_selected(unchecked))
        self.assertTrue(_source_is_selected(unknown))

    def test_list_writable_calendars_filters(self) -> None:
        def _source(
            *,
            uid: str,
            name: str,
            selected: bool | None = True,
            writable: bool = True,
        ) -> MagicMock:
            source = MagicMock()
            source.get_writable.return_value = writable
            source.get_display_name.return_value = name
            source.get_uid.return_value = uid
            source.get_parent.return_value = None
            source.has_extension.return_value = False
            if selected is None:
                source.get_extension.return_value = None
            else:
                source.get_extension.return_value.get_selected.return_value = selected
                source.get_extension.return_value.get_backend_name.return_value = ""
            return source

        work = _source(uid="work-uid", name="Work", selected=True)
        family = _source(uid="family-uid", name="Familie", selected=False)
        birthdays = _source(uid="bday-uid", name="Birthdays", selected=True)
        disabled = _source(uid="disabled-uid", name="Old job", selected=True)

        registry = MagicMock()
        registry.list_sources.return_value = [work, family, birthdays, disabled]
        registry.check_enabled.side_effect = lambda source: source is not disabled

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
