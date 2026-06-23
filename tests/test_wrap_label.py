# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.wrap_label import WrappingLabel

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


if __name__ == "__main__":
    unittest.main()
