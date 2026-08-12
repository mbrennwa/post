# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Detect and parse calendar invites from MIME and message bodies.

Detection only — never writes calendars or opens handlers.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .quote_history import unquoted_html, unquoted_plain
from .subject_prefixes import subject_looks_like_forward

_CALENDAR_MIME_TYPES = frozenset(
    {
        "text/calendar",
        "application/ics",
        "text/x-vcalendar",
    }
)

# Small Windows Outlook TZID → IANA map (common European / US names).
_WINDOWS_TZID_ALIASES: dict[str, str] = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GTB Standard Time": "Europe/Bucharest",
    "FLE Standard Time": "Europe/Helsinki",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Eastern Standard Time": "America/Indianapolis",
    "US Mountain Standard Time": "America/Phoenix",
    "Tokyo Standard Time": "Asia/Tokyo",
    "China Standard Time": "Asia/Shanghai",
    "India Standard Time": "Asia/Kolkata",
}

_TZID_VENDOR_PREFIXES = (
    "freeassociation.sourceforge.net/",
    "/freeassociation.sourceforge.net/",
)

_MEETING_URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

_MEETING_HOST_MARKERS = (
    "teams.microsoft.com",
    "teams.live.com",
    "zoom.us",
    "zoom.com",
    "meet.google.com",
    "webex.com",
    "gotomeeting.com",
    "meet.jit.si",
)

_TEAMS_PROP_KEYS = (
    "X-MICROSOFT-SKYPETEAMSMEETINGURL",
    "X-MICROSOFT-SKYPETEAMSMEETINGURL2",
)

# iTIP methods that are not "add this event" (RSVP / cancel / counter-propose).
_NON_ADDABLE_ICS_METHODS = frozenset({"REPLY", "CANCEL", "COUNTER"})

_ICS_PROP_RE = re.compile(
    r"^([A-Z0-9\-]+)((?:;[^:]*)?):(.*)$",
    re.IGNORECASE,
)


def is_calendar_mime(mime_type: str | None) -> bool:
    """True when *mime_type* is a calendar invite payload."""
    if not mime_type:
        return False
    base = str(mime_type).split(";", 1)[0].strip().lower()
    return base in _CALENDAR_MIME_TYPES


def default_calendar_filename(mime_type: str | None) -> str:
    """Filename to use when a calendar part has none."""
    base = (mime_type or "").split(";", 1)[0].strip().lower()
    if base == "text/x-vcalendar":
        return "invite.vcs"
    return "invite.ics"


def unfold_ics(text: str) -> str:
    """Unfold RFC 5545 line continuations."""
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def _unescape_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _extract_tzid(params: str) -> str | None:
    for part in params.split(";"):
        part = part.strip()
        if part.upper().startswith("TZID="):
            return part.split("=", 1)[1].strip().strip('"') or None
    return None


def _normalize_tzid(tzid: str) -> str:
    key = tzid.strip().strip('"')
    for prefix in _TZID_VENDOR_PREFIXES:
        if key.lower().startswith(prefix.lower()):
            key = key[len(prefix) :]
            break
    return _WINDOWS_TZID_ALIASES.get(key, key)


def _zoneinfo_for_tzid(tzid: str) -> ZoneInfo | None:
    key = _normalize_tzid(tzid)
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def _viewer_tzinfo() -> tzinfo:
    """Timezone used for display and for interpreting floating / local times."""
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def viewer_tzinfo() -> tzinfo:
    """Public alias: viewer local timezone (honours ``TZ`` when set)."""
    return _viewer_tzinfo()


def to_utc_iso(dt: datetime) -> str:
    """Serialize *dt* as UTC ISO seconds; naive values are viewer-local."""
    return _as_utc(dt).isoformat(timespec="seconds")


def _parse_iso_datetime_string(text: str) -> datetime | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _as_utc(dt: datetime) -> datetime:
    """Convert *dt* to UTC; naive values are treated as viewer-local."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_viewer_tzinfo())
    return dt.astimezone(timezone.utc)


def _to_display_local(dt: datetime) -> datetime:
    """Convert *dt* to the viewer’s local zone; naive stays as local wall time."""
    local_tz = _viewer_tzinfo()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def _zone_label(dt: datetime) -> str:
    """Human label for the zone used to display *dt* (already localized)."""
    abbr = (dt.strftime("%Z") or "").strip()
    tz = dt.tzinfo
    key = getattr(tz, "key", None) if tz is not None else None
    if not key:
        env_tz = os.environ.get("TZ")
        if env_tz and ("/" in env_tz or env_tz.upper() == "UTC"):
            key = env_tz
    if not key and dt.tzinfo is not None:
        offset = dt.utcoffset()
        if offset is not None:
            total = int(offset.total_seconds())
            sign = "+" if total >= 0 else "-"
            total = abs(total)
            key = f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"
    if abbr and key:
        return f"{abbr} ({key})"
    return abbr or key or ""


def _format_local_clock(dt: datetime) -> str:
    """``YYYY-MM-DD HH:MM`` (or with non-zero seconds)."""
    date_part = dt.strftime("%Y-%m-%d")
    if dt.second:
        return f"{date_part} {dt.strftime('%H:%M:%S')}"
    return f"{date_part} {dt.strftime('%H:%M')}"


def _parse_ics_datetime(value: str, params: str) -> datetime | date | None:
    raw = value.strip()
    if not raw:
        return None
    tzid = _extract_tzid(params)
    try:
        if raw.endswith("Z") and "T" in raw:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if "T" in raw:
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
            if tzid:
                zone = _zoneinfo_for_tzid(tzid)
                if zone is not None:
                    # DST-aware: wall time in named zone → absolute UTC.
                    return dt.replace(tzinfo=zone).astimezone(timezone.utc)
                # Unknown TZID: keep naive floating (do not invent UTC).
                return dt.replace(tzinfo=None)
            return dt
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None


def _parse_duration(value: str) -> timedelta | None:
    """Parse a subset of ISO-8601 durations used in ICS (e.g. PT1H30M)."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
        value.strip().upper(),
    )
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _datetime_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


def parse_ics_invite(ics_text: str) -> dict[str, Any] | None:
    """Parse the first VEVENT from ICS text into an invite dict."""
    if not ics_text or "BEGIN:VEVENT" not in ics_text.upper():
        # Still allow VCALENDAR without VEVENT markers for METHOD-only probes.
        if not ics_text or "BEGIN:VCALENDAR" not in ics_text.upper():
            return None

    unfolded = unfold_ics(ics_text)
    in_event = False
    props: dict[str, tuple[str, str]] = {}
    for line in unfolded.split("\n"):
        stripped = line.strip("\r")
        upper = stripped.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            props = {}
            continue
        if upper == "END:VEVENT":
            break
        if not in_event:
            continue
        match = _ICS_PROP_RE.match(stripped)
        if not match:
            continue
        name = match.group(1).upper()
        params = match.group(2) or ""
        value = match.group(3)
        # First occurrence wins for standard fields; keep Teams URL props.
        if name not in props or name.startswith("X-MICROSOFT-"):
            props[name] = (params, value)

    if not props and "BEGIN:VEVENT" in ics_text.upper():
        return None

    summary = _unescape_ics_text(props.get("SUMMARY", ("", ""))[1]) if "SUMMARY" in props else ""
    location = (
        _unescape_ics_text(props.get("LOCATION", ("", ""))[1]) if "LOCATION" in props else ""
    )
    description = (
        _unescape_ics_text(props.get("DESCRIPTION", ("", ""))[1])
        if "DESCRIPTION" in props
        else ""
    )
    organizer_raw = props.get("ORGANIZER", ("", ""))[1] if "ORGANIZER" in props else ""
    organizer = organizer_raw
    if organizer.upper().startswith("MAILTO:"):
        organizer = organizer[7:]
    organizer = _unescape_ics_text(organizer)

    start = None
    end = None
    event_tzid: str | None = None
    if "DTSTART" in props:
        start = _parse_ics_datetime(props["DTSTART"][1], props["DTSTART"][0])
        raw_tzid = _extract_tzid(props["DTSTART"][0])
        if raw_tzid and _zoneinfo_for_tzid(raw_tzid) is not None:
            event_tzid = _normalize_tzid(raw_tzid)
    if "DTEND" in props:
        end = _parse_ics_datetime(props["DTEND"][1], props["DTEND"][0])
    elif "DURATION" in props and isinstance(start, datetime):
        duration = _parse_duration(props["DURATION"][1])
        if duration is not None:
            end = start + duration

    meeting_url = ""
    if "URL" in props:
        meeting_url = _unescape_ics_text(props["URL"][1]).strip()
    for key in _TEAMS_PROP_KEYS:
        if key in props:
            candidate = _unescape_ics_text(props[key][1]).strip()
            if candidate:
                meeting_url = candidate
                break
    if not meeting_url:
        meeting_url = find_meeting_url(f"{location}\n{description}") or ""
    if not meeting_url and location.lower().startswith(("https://", "http://")):
        meeting_url = location.strip()

    method = ""
    method_match = re.search(r"^METHOD:(.*)$", unfolded, re.MULTILINE | re.IGNORECASE)
    if method_match:
        method = method_match.group(1).strip()

    if not (summary or start or meeting_url or location):
        return None

    all_day = False
    if start is not None and isinstance(start, date) and not isinstance(start, datetime):
        all_day = True
        start_iso = start.isoformat()
        end_iso = (
            end.isoformat()
            if isinstance(end, date) and not isinstance(end, datetime)
            else _datetime_to_iso(end)
        )
    else:
        start_iso = _datetime_to_iso(start)
        end_iso = _datetime_to_iso(end)

    return {
        "title": summary or None,
        "start": start_iso,
        "end": end_iso,
        "all_day": all_day,
        "tzid": event_tzid,
        "location": location or None,
        "organizer": organizer or None,
        "description": description or None,
        "meeting_url": meeting_url or None,
        "method": method or None,
        "source": "ics",
    }


def _is_meeting_url(url: str) -> bool:
    lower = url.lower()
    if not lower.startswith(("https://", "http://")):
        return False
    return any(marker in lower for marker in _MEETING_HOST_MARKERS)


def find_meeting_url(text: str | None) -> str | None:
    """Return the first Teams/Zoom/Meet/Webex-style URL in *text*."""
    if not text:
        return None
    for match in _MEETING_URL_RE.finditer(text):
        url = match.group(0).rstrip(").,;]>\"'")
        # Decode common HTML entities left in plain extracts.
        url = (
            url.replace("&amp;", "&")
            .replace("&quot;", "")
            .replace("&#39;", "")
        )
        if _is_meeting_url(url):
            return unquote(url) if "%" in url else url
    return None


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value.strip())


def find_meeting_url_in_html(html: str | None) -> str | None:
    """Prefer meeting URLs from anchor hrefs, then raw HTML text."""
    if not html:
        return None
    collector = _HrefCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:
        collector.hrefs = []
    for href in collector.hrefs:
        if _is_meeting_url(href):
            return href
    return find_meeting_url(html)


def invite_join_url(invite: dict[str, Any]) -> str | None:
    """Best join/open URL to show in the invite panel.

    Prefers an explicit meeting URL, then an http(s) LOCATION, then a
    Teams/Zoom-style URL found in location/description.
    """
    meeting_url = invite.get("meeting_url")
    if isinstance(meeting_url, str) and meeting_url.strip():
        return meeting_url.strip()

    location = invite.get("location")
    if isinstance(location, str):
        loc = location.strip()
        if loc.lower().startswith(("https://", "http://")):
            return loc
        found = find_meeting_url(loc)
        if found:
            return found

    description = invite.get("description")
    if isinstance(description, str):
        found = find_meeting_url(description)
        if found:
            return found
    return None


def _ics_method(ics_text: str | None) -> str:
    """Return the ICS METHOD value, uppercased, or empty when absent."""
    if not ics_text:
        return ""
    match = re.search(
        r"^METHOD:\s*(.*)$",
        unfold_ics(ics_text),
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip().upper() if match else ""


def _meeting_url_from_bodies(
    *,
    subject: str | None,
    body_plain: str | None,
    body_html: str | None,
) -> str | None:
    """Find a meeting URL, ignoring quoted history unless *subject* is a forward."""
    if subject_looks_like_forward(subject):
        return find_meeting_url_in_html(body_html) or find_meeting_url(body_plain)
    if body_html:
        # HTML present: do not fall back to the full plain body (quoted links).
        return find_meeting_url_in_html(unquoted_html(body_html))
    return find_meeting_url(unquoted_plain(body_plain))


def merge_invite_details(
    *,
    subject: str | None = None,
    ics_text: str | None = None,
    body_plain: str | None = None,
    body_html: str | None = None,
    attachment_index: int | None = None,
) -> dict[str, Any] | None:
    """Build a ``calendar_invite`` dict from ICS and/or meeting links.

    Never invents start/end times when only a meeting link is present.
    Ignores quoted reply history for meeting links unless the subject is a
    forward. ICS ``REPLY`` / ``CANCEL`` / ``COUNTER`` are not addable invites.
    """
    if _ics_method(ics_text) in _NON_ADDABLE_ICS_METHODS:
        return None

    invite: dict[str, Any] | None = None
    if ics_text:
        invite = parse_ics_invite(ics_text)

    meeting_url = None
    if invite and invite.get("meeting_url"):
        meeting_url = invite["meeting_url"]
    if not meeting_url:
        meeting_url = _meeting_url_from_bodies(
            subject=subject,
            body_plain=body_plain,
            body_html=body_html,
        )

    if invite is None and meeting_url:
        invite = {
            "title": (subject or "").strip() or None,
            "start": None,
            "end": None,
            "all_day": False,
            "tzid": None,
            "location": None,
            "organizer": None,
            "description": None,
            "meeting_url": meeting_url,
            "method": None,
            "source": "meeting_link",
        }
    elif invite is not None:
        if not invite.get("meeting_url") and meeting_url:
            invite["meeting_url"] = meeting_url
        if not invite.get("title") and subject:
            invite["title"] = subject.strip() or None
        invite["source"] = invite.get("source") or "ics"

    if invite is None:
        return None

    if attachment_index is not None:
        invite["attachment_index"] = attachment_index
    return invite


def email_part_counts_as_attachment(part: Any) -> bool:
    """Match attachment heuristics used by helpers email fallback."""
    ctype = part.get_content_type()
    if ctype.startswith("multipart/"):
        return False
    if is_calendar_mime(ctype):
        return True
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    if disposition != "attachment" and not filename:
        return False
    if disposition == "inline" and ctype.startswith("text/") and not is_calendar_mime(ctype):
        return False
    # CID / inline images are shown in the body, not the attachment list (#258).
    if ctype.startswith("image/") and disposition != "attachment":
        content_id = part.get("Content-ID")
        if content_id or disposition == "inline":
            return False
    return True


def extract_ics_text_from_email_message(msg: Any) -> tuple[str | None, int | None]:
    """Return (ics_text, attachment_index) from an ``email.message`` object."""
    index = -1
    first_text: str | None = None
    first_index: int | None = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("multipart/"):
            continue
        if not email_part_counts_as_attachment(part):
            continue
        index += 1
        if not is_calendar_mime(ctype):
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if first_text is None:
            first_text = text
            first_index = index
        # Prefer METHOD:REQUEST when multiple calendar parts exist.
        if re.search(r"^METHOD:\s*REQUEST\s*$", text, re.MULTILINE | re.IGNORECASE):
            return text, index
    return first_text, first_index


def calendar_invite_from_email_message(
    msg: Any,
    *,
    subject: str | None = None,
    body_plain: str | None = None,
    body_html: str | None = None,
) -> dict[str, Any] | None:
    """Build invite details from a Python ``email.message`` (tests / fallback)."""
    ics_text, attachment_index = extract_ics_text_from_email_message(msg)
    if body_plain is None or body_html is None:
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and body_plain is None:
                try:
                    body_plain = part.get_content()
                    if isinstance(body_plain, bytes):
                        body_plain = body_plain.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    body_plain = payload.decode("utf-8", errors="replace")
            elif ctype == "text/html" and body_html is None:
                try:
                    body_html = part.get_content()
                    if isinstance(body_html, bytes):
                        body_html = body_html.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    body_html = payload.decode("utf-8", errors="replace")
    if subject is None:
        subject = msg.get("Subject")
    return merge_invite_details(
        subject=subject,
        ics_text=ics_text,
        body_plain=body_plain,
        body_html=body_html,
        attachment_index=attachment_index,
    )


def format_invite_when(invite: dict[str, Any]) -> str | None:
    """Human-readable when line for the reader panel.

    Converts aware timestamps to the viewer’s local zone (DST-aware). Naive
    times are treated as already local. Appends a zone label such as
    ``CEST (Europe/Zurich)``. Same-day ranges render as
    ``YYYY-MM-DD HH:MM – HH:MM …``. All-day events omit the zone label.
    """
    start = invite.get("start")
    end = invite.get("end")
    if not start:
        return None

    if invite.get("all_day"):
        start_d = str(start).strip()[:10]
        end_d = str(end).strip()[:10] if end else None
        if end_d and end_d != start_d:
            return f"{start_d} – {end_d} (all day)"
        return f"{start_d} (all day)"

    start_dt = _parse_iso_datetime_string(str(start))
    if start_dt is None:
        return str(start).strip()

    start_local = _to_display_local(start_dt)
    zone = _zone_label(start_local)
    start_disp = _format_local_clock(start_local)

    end_dt = _parse_iso_datetime_string(str(end)) if end else None
    if end_dt is None:
        return f"{start_disp} {zone}".rstrip() if zone else start_disp

    end_local = _to_display_local(end_dt)
    end_disp = _format_local_clock(end_local)
    start_date, _, start_time = start_disp.partition(" ")
    end_date, _, end_time = end_disp.partition(" ")
    if start_date and end_date and start_date == end_date and start_time and end_time:
        when = f"{start_date} {start_time} – {end_time}"
    else:
        when = f"{start_disp} – {end_disp}"
    return f"{when} {zone}".rstrip() if zone else when


def format_invite_copy_text(invite: dict[str, Any]) -> str:
    """Plain-text invite summary for clipboard paste.

    Intentionally omits ICS DESCRIPTION — Teams/Outlook often embed the full
    message body there, which must not be copied with the invite details.
    """
    lines: list[str] = []
    title = invite.get("title")
    if title:
        lines.append(str(title))
    when = format_invite_when(invite)
    if when:
        lines.append(f"When: {when}")
    elif invite.get("start"):
        lines.append(f"When: {invite.get('start')}")
    location = invite.get("location")
    join = invite_join_url(invite)
    if location and (not join or str(location).rstrip("/") != join.rstrip("/")):
        lines.append(f"Where: {location}")
    organizer = invite.get("organizer")
    if organizer:
        lines.append(f"Organizer: {organizer}")
    if join:
        lines.append(f"Link: {join}")
    return "\n".join(lines).strip() + "\n"


def invite_link_label(url: str) -> str:
    """Short visible label for a meeting URL (full URL stays in tooltip/href)."""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).netloc or "").strip()
    except Exception:
        host = ""
    if host:
        return f"Link: {host}"
    return "Link: Open meeting"


def build_vevent_ics(invite: dict[str, Any]) -> str:
    """Build a VEVENT string suitable for ``ECal.Client.create_object``.

    Includes UID and DTSTAMP. Returns a single VEVENT component (not a
    surrounding VCALENDAR), which is what EDS expects. Timed events are
    always written as UTC (``…Z``).
    """
    import uuid

    def esc(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    title = invite.get("title") or "Meeting"
    start = invite.get("start")
    end = invite.get("end")
    if not start:
        raise ValueError("Invite is missing a start time")

    def to_ics_stamp(iso_value: str, all_day: bool) -> str:
        cleaned = iso_value.strip()
        if all_day or "T" not in cleaned:
            return cleaned[:10].replace("-", "")
        dt = _parse_iso_datetime_string(cleaned)
        if dt is None:
            raise ValueError(f"Invalid invite datetime: {iso_value!r}")
        return _as_utc(dt).strftime("%Y%m%dT%H%M%SZ")

    all_day = bool(invite.get("all_day"))
    dtstart = to_ics_stamp(str(start), all_day)
    uid = str(invite.get("uid") or uuid.uuid4())
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"SUMMARY:{esc(str(title))}",
    ]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        if end:
            lines.append(f"DTEND;VALUE=DATE:{to_ics_stamp(str(end), True)}")
    else:
        lines.append(f"DTSTART:{dtstart}")
        if end:
            lines.append(f"DTEND:{to_ics_stamp(str(end), False)}")

    location = invite.get("location")
    meeting_url = invite.get("meeting_url")
    location_text = str(location).strip() if location else ""
    meeting_url_text = str(meeting_url).strip() if meeting_url else ""
    # Prefer the join URL in LOCATION so calendar apps show a Join button.
    # Keep any non-URL place label for DESCRIPTION when it would otherwise be lost.
    preserved_location = ""
    if meeting_url_text:
        lines.append(f"LOCATION:{esc(meeting_url_text)}")
        lines.append(f"URL:{esc(meeting_url_text)}")
        if (
            location_text
            and not location_text.lower().startswith(("https://", "http://"))
            and location_text.rstrip("/") != meeting_url_text.rstrip("/")
        ):
            preserved_location = location_text
    elif location_text:
        lines.append(f"LOCATION:{esc(location_text)}")
    description = invite.get("description")
    desc_text = str(description) if description else ""
    desc_parts: list[str] = []
    if preserved_location and preserved_location not in desc_text:
        desc_parts.append(preserved_location)
    if desc_text:
        desc_parts.append(desc_text)
    if meeting_url_text and meeting_url_text not in desc_text:
        desc_parts.append(meeting_url_text)
    if desc_parts:
        lines.append(f"DESCRIPTION:{esc(chr(10).join(desc_parts))}")
    organizer = invite.get("organizer")
    if organizer:
        lines.append(f"ORGANIZER:MAILTO:{esc(str(organizer))}")
    lines.extend(["END:VEVENT", ""])
    return "\r\n".join(lines)
