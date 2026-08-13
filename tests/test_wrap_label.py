# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import GLib, Gtk, Pango

from post.preferences import MIN_MESSAGE_LIST_WIDTH
from post.wrap_label import (
    WrappingLabel,
    configure_ellipsize_label,
    configure_pane_scrolled_window,
)

_STATUS_ICON_PX = 48
_STATUS_MARGIN_PX = 24

_LONG_LINE = (
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)


class WrappingLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def test_wrap_mode_accepts_gtk_enum(self) -> None:
        label = WrappingLabel(
            label="hello",
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self.assertEqual(label.get_wrap_mode(), Pango.WrapMode.WORD_CHAR)

    def test_horizontal_measure_without_constraint_is_zero(self) -> None:
        label = WrappingLabel(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = label.measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)

    def test_horizontal_measure_ignores_opposite_size_constraint(self) -> None:
        # For HORIZONTAL measure, for_size is a height, not a width. Claiming
        # natural width == for_size makes parents report natural < min.
        label = WrappingLabel(label=_LONG_LINE, wrap=True)
        minimum, natural, _, _ = label.measure(Gtk.Orientation.HORIZONTAL, 200)
        self.assertEqual(minimum, 0)
        self.assertEqual(natural, 0)

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

    def test_reader_header_row_natural_width_at_least_minimum(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_hexpand(True)
        subject = WrappingLabel(
            label="A fairly long subject that should wrap in the reader",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        subject.set_max_width_chars(1)
        subject.set_hexpand(True)
        subject.set_halign(Gtk.Align.FILL)
        subject_box = Gtk.Box()
        subject_box.set_hexpand(True)
        subject_box.append(subject)
        header.append(subject_box)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        actions.add_css_class("linked")
        for icon in (
            "mail-reply-sender-symbolic",
            "mail-reply-all-symbolic",
            "mail-forward-symbolic",
        ):
            button = Gtk.Button()
            button.set_icon_name(icon)
            actions.append(button)
        header.append(actions)

        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.append(header)
        for for_size in (-1, 124, 649):
            minimum, natural, _, _ = pane.measure(Gtk.Orientation.HORIZONTAL, for_size)
            self.assertGreaterEqual(
                natural,
                minimum,
                f"for_size={for_size} natural={natural} min={minimum}",
            )

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
        attachment = Gtk.Box()
        attachment.set_size_request(16, 16)
        row.append(attachment)
        minimum, natural, _, _ = row.measure(Gtk.Orientation.HORIZONTAL, 649)
        self.assertGreaterEqual(natural, minimum)

    def test_centered_status_box_collapses_wrapping_label(self) -> None:
        # WrappingLabel claims 0 width; CENTER parents size to the icon (#293).
        label, window = self._realize_status_note(box_halign=Gtk.Align.CENTER)
        try:
            self.assertLessEqual(label.get_width(), _STATUS_ICON_PX)
        finally:
            window.destroy()

    def test_fill_status_box_gives_wrapping_label_pane_width(self) -> None:
        label, window = self._realize_status_note(box_halign=Gtk.Align.FILL)
        try:
            pane_width = label.get_parent().get_parent().get_width()
            self.assertGreater(pane_width, _STATUS_ICON_PX)
            self.assertGreater(label.get_width(), _STATUS_ICON_PX)
            self.assertEqual(
                label.get_width(),
                pane_width - (2 * _STATUS_MARGIN_PX),
            )
        finally:
            window.destroy()

    def _realize_status_note(
        self, *, box_halign: Gtk.Align
    ) -> tuple[WrappingLabel, Gtk.Window]:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=box_halign,
            valign=Gtk.Align.CENTER,
        )
        if box_halign == Gtk.Align.FILL:
            box.set_hexpand(True)
        box.set_margin_start(_STATUS_MARGIN_PX)
        box.set_margin_end(_STATUS_MARGIN_PX)
        icon = Gtk.Box()
        icon.set_size_request(_STATUS_ICON_PX, _STATUS_ICON_PX)
        icon.set_halign(Gtk.Align.CENTER)
        box.append(icon)
        label = WrappingLabel(
            label="No Messages in INBOX",
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD,
        )
        if box_halign == Gtk.Align.FILL:
            label.set_halign(Gtk.Align.FILL)
            label.set_hexpand(True)
            label.set_justify(Gtk.Justification.CENTER)
        box.append(label)

        stack = Gtk.Stack()
        stack.set_size_request(MIN_MESSAGE_LIST_WIDTH, -1)
        stack.set_hexpand(True)
        stack.add_named(box, "empty")
        stack.set_visible_child_name("empty")

        window = Gtk.Window()
        window.set_default_size(MIN_MESSAGE_LIST_WIDTH, 400)
        window.set_child(stack)
        window.present()
        context = GLib.MainContext.default()
        for _ in range(50):
            if not context.iteration(False):
                break
        return label, window

    def test_pane_scrolled_window_pins_horizontal_adjustment(self) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        child = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        child.set_size_request(800, 40)
        scroll.set_child(child)
        configure_pane_scrolled_window(scroll)
        adj = scroll.get_hadjustment()
        self.assertIsNotNone(adj)
        assert adj is not None
        adj.set_lower(0)
        adj.set_upper(800)
        adj.set_page_size(120)
        adj.set_value(200)
        self.assertEqual(adj.get_value(), 0)


if __name__ == "__main__":
    unittest.main()
