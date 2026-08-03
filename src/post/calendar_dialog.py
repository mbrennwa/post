# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Dialog to pick an EDS calendar and confirm invite details before writing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from post.mail.calendar_write import (
    CalendarTarget,
    add_invite_to_calendar,
    ecal_available,
    list_writable_calendars,
)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


class AddToCalendarDialog:
    """Present calendar chooser (+ optional time fields) then write on confirm."""

    def __init__(
        self,
        parent: Gtk.Window,
        invite: dict[str, Any],
        *,
        on_success: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        run_async: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._parent = parent
        self._invite = dict(invite)
        self._on_success = on_success
        self._on_error = on_error
        self._run_async = run_async
        self._targets: list[CalendarTarget] = []
        self._calendar_combo: Gtk.DropDown | None = None
        self._start_entry: Gtk.Entry | None = None
        self._end_entry: Gtk.Entry | None = None
        self._dialog: Adw.AlertDialog | None = None

    def present(self) -> None:
        if not ecal_available():
            if self._on_error:
                self._on_error(
                    "Calendar support is unavailable. Install gir1.2-ecal-2.0 "
                    "(or the equivalent package for your distribution)."
                )
            return

        try:
            self._targets = list_writable_calendars()
        except Exception as exc:
            if self._on_error:
                self._on_error(f"Could not list calendars: {exc}")
            return

        if not self._targets:
            if self._on_error:
                self._on_error("No writable calendars found.")
            return

        needs_time = not self._invite.get("start")
        body_lines = [
            self._invite.get("title") or "Meeting",
        ]
        if self._invite.get("meeting_url"):
            body_lines.append(str(self._invite["meeting_url"]))
        if needs_time:
            body_lines.append(
                "This invite has no start time. Enter when it happens, "
                "then choose a calendar."
            )
        else:
            body_lines.append("Choose which calendar should receive this event.")

        dialog = Adw.AlertDialog(
            heading="Add to Calendar",
            body="\n".join(body_lines),
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(8)

        labels = [target.label for target in self._targets]
        string_list = Gtk.StringList.new(labels)
        combo = Gtk.DropDown.new(string_list, None)
        combo.set_selected(0)
        self._calendar_combo = combo
        cal_label = Gtk.Label(label="Calendar", xalign=0)
        cal_label.add_css_class("caption-heading")
        content.append(cal_label)
        content.append(combo)

        if needs_time:
            start_label = Gtk.Label(label="Start (YYYY-MM-DD HH:MM)", xalign=0)
            start_label.add_css_class("caption-heading")
            content.append(start_label)
            self._start_entry = Gtk.Entry()
            self._start_entry.set_placeholder_text("2026-08-03 15:00")
            content.append(self._start_entry)
            end_label = Gtk.Label(label="End (optional)", xalign=0)
            end_label.add_css_class("caption-heading")
            content.append(end_label)
            self._end_entry = Gtk.Entry()
            self._end_entry.set_placeholder_text("2026-08-03 16:00")
            content.append(self._end_entry)

        dialog.set_extra_child(content)
        dialog.connect("response", self._on_response)
        self._dialog = dialog
        dialog.present(self._parent)

    def _on_response(self, dialog: Adw.AlertDialog, response: str) -> None:
        if response != "add":
            return
        if self._calendar_combo is None or not self._targets:
            return
        selected = int(self._calendar_combo.get_selected())
        if selected < 0 or selected >= len(self._targets):
            if self._on_error:
                self._on_error("No calendar selected")
            return
        target = self._targets[selected]
        invite = dict(self._invite)

        if self._start_entry is not None:
            start_text = self._start_entry.get_text().strip()
            end_text = (
                self._end_entry.get_text().strip() if self._end_entry is not None else ""
            )
            start_dt = _parse_user_datetime(start_text)
            if start_dt is None:
                if self._on_error:
                    self._on_error("Enter a valid start date/time (YYYY-MM-DD HH:MM)")
                return
            invite["start"] = start_dt.isoformat(timespec="seconds")
            invite["all_day"] = False
            if end_text:
                end_dt = _parse_user_datetime(end_text)
                if end_dt is None:
                    if self._on_error:
                        self._on_error("Enter a valid end date/time (YYYY-MM-DD HH:MM)")
                    return
                invite["end"] = end_dt.isoformat(timespec="seconds")
            else:
                invite["end"] = (start_dt + timedelta(hours=1)).isoformat(
                    timespec="seconds"
                )

        def worker() -> None:
            error: Exception | None = None
            try:
                add_invite_to_calendar(target.uid, invite)
            except Exception as exc:
                error = exc

            def done() -> bool:
                if error is not None:
                    if self._on_error:
                        self._on_error(str(error))
                elif self._on_success:
                    self._on_success(target.label)
                return False

            GLib.idle_add(done)

        if self._run_async is not None:
            self._run_async(worker)
        else:
            worker()


def _parse_user_datetime(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return _parse_iso_datetime(text)


def present_add_to_calendar(
    parent: Gtk.Window,
    invite: dict[str, Any],
    *,
    on_success: Callable[[str], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    run_async: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    """Convenience wrapper around :class:`AddToCalendarDialog`."""
    AddToCalendarDialog(
        parent,
        invite,
        on_success=on_success,
        on_error=on_error,
        run_async=run_async,
    ).present()
