# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared attachment context-menu actions and popover wiring."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, Gtk

from post.mail.calendar_invite import looks_like_calendar_attachment

SAVE_ACTION = "attachment-save"
OPEN_WITH_ACTION = "attachment-open-with"
ADD_CALENDAR_ACTION = "attachment-add-to-calendar"

LABEL_SAVE = "Save..."
LABEL_OPEN_WITH = "Open With…"
LABEL_ADD_CALENDAR = "Add to Calendar…"

_ACTION_PREFIX = "win"


def _detailed_action(name: str) -> str:
    return f"{_ACTION_PREFIX}.{name}"


def register_attachment_actions(
    action_map: Gio.ActionMap,
    *,
    on_save: Callable[..., Any],
    on_open_with: Callable[..., Any],
    on_add_to_calendar: Callable[..., Any] | None = None,
) -> None:
    """Register Save / Open With / optional Add to Calendar window actions."""
    save_action = Gio.SimpleAction.new(SAVE_ACTION, None)
    save_action.connect("activate", on_save)
    action_map.add_action(save_action)

    open_with_action = Gio.SimpleAction.new(OPEN_WITH_ACTION, None)
    open_with_action.connect("activate", on_open_with)
    action_map.add_action(open_with_action)

    if on_add_to_calendar is not None:
        add_cal_action = Gio.SimpleAction.new(ADD_CALENDAR_ACTION, None)
        add_cal_action.connect("activate", on_add_to_calendar)
        action_map.add_action(add_cal_action)


def build_attachment_menu(*, include_calendar: bool = False) -> Gio.Menu:
    """Build the attachment context menu model."""
    menu = Gio.Menu()
    menu.append(LABEL_SAVE, _detailed_action(SAVE_ACTION))
    menu.append(LABEL_OPEN_WITH, _detailed_action(OPEN_WITH_ACTION))
    if include_calendar:
        menu.append(LABEL_ADD_CALENDAR, _detailed_action(ADD_CALENDAR_ACTION))
    return menu


def make_attachment_popover() -> Gtk.PopoverMenu:
    """Create a popover seeded with the base Save / Open With menu."""
    return Gtk.PopoverMenu.new_from_model(build_attachment_menu())


def ensure_popover_parent(popover: Gtk.PopoverMenu, widget: Gtk.Widget) -> None:
    """Reparent a popover onto *widget*, unparenting any previous parent."""
    current = popover.get_parent()
    if current is widget:
        return
    if current is not None:
        popover.popdown()
        if popover.get_parent() is current:
            popover.unparent()
    popover.set_parent(widget)


def popup_attachment_menu(
    popover: Gtk.PopoverMenu,
    widget: Gtk.Widget,
    x: float,
    y: float,
    *,
    mime_type: str | None = None,
    name: str = "",
    support_calendar: bool = False,
) -> None:
    """Show the attachment menu at (*x*, *y*) relative to *widget*."""
    include_calendar = False
    if support_calendar:
        include_calendar = looks_like_calendar_attachment(mime_type, name)
    popover.set_menu_model(build_attachment_menu(include_calendar=include_calendar))
    ensure_popover_parent(popover, widget)
    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = 1
    rect.height = 1
    popover.set_pointing_to(rect)
    popover.popup()
