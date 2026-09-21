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


def dismiss_popover(popover: Gtk.Popover | None) -> None:
    """Popdown and unparent *popover* if it still has a parent (sidebar-style)."""
    if popover is None:
        return
    if popover.get_visible():
        popover.popdown()
    parent = popover.get_parent()
    if parent is not None:
        popover.unparent()


def popup_attachment_menu(
    popover: Gtk.PopoverMenu | None,
    widget: Gtk.Widget,
    x: float,
    y: float,
    *,
    mime_type: str | None = None,
    name: str = "",
    support_calendar: bool = False,
) -> Gtk.PopoverMenu:
    """Show a fresh attachment menu at (*x*, *y*) relative to *widget*.

    Dismisses any previous *popover* and returns the new instance so callers
    can keep a reference without calling ``set_menu_model`` on a live popover.
    """
    dismiss_popover(popover)
    include_calendar = False
    if support_calendar:
        include_calendar = looks_like_calendar_attachment(mime_type, name)
    new_popover = Gtk.PopoverMenu.new_from_model(
        build_attachment_menu(include_calendar=include_calendar)
    )
    new_popover.set_parent(widget)
    rect = Gdk.Rectangle()
    rect.x = int(x)
    rect.y = int(y)
    rect.width = 1
    rect.height = 1
    new_popover.set_pointing_to(rect)
    new_popover.popup()
    return new_popover
