# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.wrap_label import WrappingLabel, configure_ellipsize_label

_LONG_LINE = (
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


class WrappingLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def test_horizontal_measure_without_constraint_is_zero(self) -> None:
        label = WrappingLabel(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = label.measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)

    def test_horizontal_measure_with_constraint_uses_width(self) -> None:
        label = WrappingLabel(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = label.measure(Gtk.Orientation.HORIZONTAL, 200)
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 200)

    def test_vertical_measure_with_width_constraint_returns_height(self) -> None:
        label = WrappingLabel(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = label.measure(Gtk.Orientation.VERTICAL, 200)
        self.assertGreater(natural, 0)
        self.assertEqual(minimum, natural)

    def test_plain_label_inflates_horizontal_natural_width(self) -> None:
        plain = Gtk.Label(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = plain.measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertGreater(natural, 200)
        self.assertGreater(minimum, 0)

    def test_message_list_row_natural_width_at_least_minimum(self) -> None:
        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        preview.set_margin_start(4)
        preview.set_margin_end(12)
        preview.set_margin_top(8)
        preview.set_margin_bottom(8)

        subject = WrappingLabel(
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            label="Quarterly review notes",
        )
        subject.set_hexpand(True)
        subject.set_halign(Gtk.Align.FILL)
        preview.append(subject)

        date_label = Gtk.Label(xalign=0, label="Mon, 23 Jun 2026 18:15:34")
        date_label.add_css_class("dim-label")
        date_label.set_halign(Gtk.Align.START)
        preview.append(date_label)

        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta = Gtk.Label(xalign=0, ellipsize=3, label="sender@example.com")
        configure_ellipsize_label(meta)
        bottom_row.append(meta)
        preview.append(bottom_row)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.set_margin_start(8)
        dot = Gtk.Box()
        dot.set_size_request(16, -1)
        outer.append(dot)
        outer.append(preview)

        for widget in (preview, outer):
            minimum, natural, _, _ = widget.measure(Gtk.Orientation.HORIZONTAL, 649)
            self.assertGreaterEqual(
                natural,
                minimum,
                f"{type(widget).__name__} natural={natural} min={minimum}",
            )

    def test_ellipsize_label_in_horizontal_box_reports_valid_width(self) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta = Gtk.Label(xalign=0, ellipsize=3, label="sender@example.com")
        configure_ellipsize_label(meta)
        row.append(meta)
        row.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
        minimum, natural, _, _ = row.measure(Gtk.Orientation.HORIZONTAL, 649)
        self.assertGreaterEqual(natural, minimum)


if __name__ == "__main__":
    unittest.main()
