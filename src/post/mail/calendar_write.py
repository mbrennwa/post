# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""List EDS calendars and write invite VEVENTs (user-triggered only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from post.mail.calendar_invite import build_vevent_ics

log = logging.getLogger("post.mail.calendar_write")

_SKIP_BACKEND_NAMES = frozenset(
    {
        "birthdays",
        "contacts-birthdays",
        "weather",
    }
)


@dataclass(frozen=True)
class CalendarTarget:
    uid: str
    display_name: str
    parent_name: str | None = None

    @property
    def label(self) -> str:
        if self.parent_name:
            return f"{self.display_name} ({self.parent_name})"
        return self.display_name


def ecal_available() -> bool:
    """True when ECal GI bindings can be imported."""
    try:
        import gi

        gi.require_version("ECal", "2.0")
        from gi.repository import ECal  # noqa: F401

        return True
    except (ImportError, ValueError, AttributeError):
        return False


def _source_backend_name(source: Any) -> str:
    try:
        ext = source.get_extension("Calendar")
        if ext is not None and hasattr(ext, "get_backend_name"):
            name = ext.get_backend_name()
            if name:
                return str(name).lower()
    except Exception:
        pass
    try:
        if source.has_extension("LocalBackend"):
            return "local"
    except Exception:
        pass
    return ""


def _source_is_selected(source: Any) -> bool:
    """True when the calendar is checked in Evolution (EDS selected).

    If the Calendar extension has no ``get_selected``, keep the source so
    odd or mocked sources are not dropped.
    """
    try:
        ext = source.get_extension("Calendar")
        if ext is not None and hasattr(ext, "get_selected"):
            return bool(ext.get_selected())
    except Exception:
        pass
    return True


def _source_is_effectively_enabled(registry: Any, source: Any) -> bool:
    """True when the source and its ancestors are enabled."""
    try:
        if hasattr(registry, "check_enabled"):
            return bool(registry.check_enabled(source))
    except Exception:
        pass
    try:
        if hasattr(source, "get_enabled"):
            return bool(source.get_enabled())
    except Exception:
        pass
    return True


def _source_is_writable(source: Any) -> bool:
    try:
        if hasattr(source, "get_writable") and not source.get_writable():
            return False
    except Exception:
        pass
    try:
        if hasattr(source, "get_remote_deletable"):
            # Presence of remote APIs is not decisive; prefer explicit writable.
            pass
    except Exception:
        pass
    backend = _source_backend_name(source)
    if backend in _SKIP_BACKEND_NAMES or "birthday" in backend:
        return False
    display = (source.get_display_name() or "").lower()
    if "birthday" in display or "anniversary" in display:
        return False
    if "holiday" in display or "feiertag" in display:
        return False
    return True


def list_writable_calendars(registry: Any | None = None) -> list[CalendarTarget]:
    """Return writable EDS calendars that are checked in Evolution."""
    import gi

    gi.require_version("EDataServer", "1.2")
    from gi.repository import EDataServer as EDS

    if registry is None:
        registry = EDS.SourceRegistry.new_sync(None)

    targets: list[CalendarTarget] = []
    for source in registry.list_sources(EDS.SOURCE_EXTENSION_CALENDAR):
        if not _source_is_effectively_enabled(registry, source):
            continue
        if not _source_is_selected(source):
            continue
        if not _source_is_writable(source):
            continue
        uid = source.get_uid()
        name = source.get_display_name() or uid
        parent_name = None
        try:
            parent_uid = source.get_parent()
            if parent_uid:
                parent = registry.ref_source(parent_uid)
                if parent is not None:
                    parent_name = parent.get_display_name()
        except Exception:
            parent_name = None
        targets.append(
            CalendarTarget(uid=str(uid), display_name=str(name), parent_name=parent_name)
        )

    targets.sort(key=lambda item: item.label.lower())
    return targets


def add_invite_to_calendar(
    calendar_uid: str,
    invite: dict[str, Any],
    *,
    registry: Any | None = None,
) -> None:
    """Create a VEVENT on the EDS calendar identified by *calendar_uid*."""
    if not invite.get("start"):
        raise ValueError("Choose a start date/time before adding to a calendar")

    import gi

    gi.require_version("EDataServer", "1.2")
    gi.require_version("ECal", "2.0")
    from gi.repository import ECal, EDataServer as EDS

    if registry is None:
        registry = EDS.SourceRegistry.new_sync(None)
    source = registry.ref_source(calendar_uid)
    if source is None:
        raise ValueError("Calendar not found")

    ics = build_vevent_ics(invite)
    client = ECal.Client.connect_sync(
        source,
        ECal.ClientSourceType.EVENTS,
        30,
        None,
    )
    if client is None:
        raise RuntimeError("Could not connect to calendar")

    gi.require_version("ICalGLib", "3.0")
    from gi.repository import ICalGLib

    component = ICalGLib.Component.new_from_string(ics)
    if component is None:
        raise RuntimeError("Could not parse calendar event")

    # ECal expects a VEVENT, not a surrounding VCALENDAR.
    try:
        vevent = component.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if vevent is not None:
            component = vevent
    except Exception:
        pass

    if not hasattr(client, "create_object_sync"):
        raise RuntimeError("Calendar client cannot create events")
    flags = getattr(getattr(ECal, "OperationFlags", None), "NONE", 0)
    try:
        ok, _out_uid = client.create_object_sync(component, flags, None)
    except TypeError:
        # Older / alternate GI shapes.
        result = client.create_object_sync(component, flags, None)
        ok = result[0] if isinstance(result, tuple) else result
    if not ok:
        raise RuntimeError("Calendar rejected the event")

    log.info("Added invite %r to calendar %s", invite.get("title"), calendar_uid)
