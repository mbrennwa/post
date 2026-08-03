# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""GTK labels that wrap correctly inside width-constrained layouts."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gtk, Pango

_GTK_TO_PANGO_WRAP: dict[Gtk.WrapMode, Pango.WrapMode] = {
    Gtk.WrapMode.WORD: Pango.WrapMode.WORD,
    Gtk.WrapMode.CHAR: Pango.WrapMode.CHAR,
    Gtk.WrapMode.WORD_CHAR: Pango.WrapMode.WORD_CHAR,
}
_pango_wrap_none = getattr(Pango.WrapMode, "NONE", None)
if _pango_wrap_none is not None:
    _GTK_TO_PANGO_WRAP[Gtk.WrapMode.NONE] = _pango_wrap_none


def _to_pango_wrap_mode(mode: Gtk.WrapMode | Pango.WrapMode) -> Pango.WrapMode:
    if isinstance(mode, Pango.WrapMode):
        return mode
    return _GTK_TO_PANGO_WRAP.get(mode, Pango.WrapMode.WORD_CHAR)


def set_label_wrap_mode(label: Gtk.Label, mode: Gtk.WrapMode | Pango.WrapMode) -> None:
    """Apply wrap mode to a Gtk.Label without passing Gtk enums to Pango."""
    label.set_wrap_mode(_to_pango_wrap_mode(mode))


def configure_ellipsize_label(label: Gtk.Label) -> Gtk.Label:
    """Keep ellipsized labels from forcing invalid GtkBox measure results."""
    if label.get_ellipsize() == Pango.EllipsizeMode.NONE:
        label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_max_width_chars(1)
    label.set_hexpand(True)
    return label


class EllipsizingLabel(Gtk.Label):
    """Label that ellipsizes without expanding its parent past the allocated width.

    Plain ``Gtk.Label`` with ``ellipsize=END`` still reports the full text as its
    natural width, which can grow a reader pane and clip trailing siblings
    (e.g. an Add button) outside the visible area.
    """

    __gtype_name__ = "PostEllipsizingLabel"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.get_ellipsize() == Pango.EllipsizeMode.NONE:
            self.set_ellipsize(Pango.EllipsizeMode.END)
        self.set_max_width_chars(1)
        self.set_hexpand(True)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(
        self, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        if orientation == Gtk.Orientation.HORIZONTAL:
            # Claim no horizontal size so parents allocate us the remaining
            # width; ellipsis then applies to that allocation.
            return 0, 0, -1, -1
        return Gtk.Label.do_measure(self, orientation, for_size)


def configure_pane_scrolled_window(scroll: Gtk.ScrolledWindow) -> Gtk.ScrolledWindow:
    """Keep pane content left-aligned and clip overflow on the right (#229).

    When a scrolled child is wider than the pane, GTK can raise the horizontal
    adjustment as the pane shrinks, which clips text on the left. Pin the
    adjustment to 0 and hide overflow so narrowing cuts off the right edge.
    """
    scroll.set_propagate_natural_width(False)
    scroll.set_overflow(Gtk.Overflow.HIDDEN)
    bound: dict[str, object | None] = {"adj": None, "value_id": None, "changed_id": None}

    def _keep_left(*_args) -> None:
        adj = scroll.get_hadjustment()
        if adj is not None and adj.get_value() != 0:
            adj.set_value(0)

    def _bind_hadjustment(*_args) -> None:
        adj = scroll.get_hadjustment()
        prev = bound["adj"]
        if prev is adj:
            _keep_left()
            return
        if prev is not None:
            value_id = bound["value_id"]
            changed_id = bound["changed_id"]
            if value_id is not None:
                prev.disconnect(value_id)
            if changed_id is not None:
                prev.disconnect(changed_id)
        bound["adj"] = adj
        bound["value_id"] = None
        bound["changed_id"] = None
        if adj is None:
            return
        bound["value_id"] = adj.connect("value-changed", _keep_left)
        bound["changed_id"] = adj.connect("changed", _keep_left)
        _keep_left()

    scroll.connect("notify::hadjustment", _bind_hadjustment)
    scroll.connect("notify::width", _keep_left)
    _bind_hadjustment()
    return scroll


class WrappingLabel(Gtk.Label):
    """A label that wraps without forcing its parent layout to grow horizontally."""

    __gtype_name__ = "WrappingLabel"

    def __init__(self, *args, **kwargs) -> None:
        wrap_mode = kwargs.pop("wrap_mode", None)
        super().__init__(*args, **kwargs)
        if wrap_mode is not None:
            self.set_wrap_mode(wrap_mode)

    def set_wrap_mode(self, mode: Gtk.WrapMode | Pango.WrapMode) -> None:
        super().set_wrap_mode(_to_pango_wrap_mode(mode))

    def _pango_wrap_mode(self) -> Pango.WrapMode:
        return self.get_wrap_mode()

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        if self.get_wrap():
            return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH
        return Gtk.Label.do_get_request_mode(self)

    def _layout_height(self, width: int) -> int:
        layout = self.get_layout()
        if width > 0:
            layout.set_width(width * Pango.SCALE)
        else:
            layout.set_width(-1)
        layout.set_wrap(self._pango_wrap_mode())
        return layout.get_pixel_size()[1]

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        if not self.get_wrap():
            return Gtk.Label.do_measure(self, orientation, for_size)

        if orientation == Gtk.Orientation.HORIZONTAL:
            # Claim no horizontal size so parents wrap us to the allocated
            # width. Returning ``for_size`` here is wrong: for a horizontal
            # measure, for_size is a height constraint, and it can make a
            # parent report natural width < min width (and crash GTK).
            return 0, 0, -1, -1

        if for_size > 0:
            height = self._layout_height(for_size)
            return height, height, -1, -1

        return Gtk.Label.do_measure(self, orientation, for_size)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.Label.do_size_allocate(self, width, height, baseline)
        if self.get_wrap() and width > 0:
            layout = self.get_layout()
            layout.set_width(width * Pango.SCALE)
            layout.set_wrap(self._pango_wrap_mode())
