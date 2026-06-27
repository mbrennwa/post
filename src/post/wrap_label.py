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
            if for_size > 0:
                return 0, for_size, -1, -1
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
