# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk

from post.layout_debug import (
    clear_registry,
    enabled,
    name_widget,
    probe_boxes,
    probe_registered,
)


class LayoutDebugTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def test_enabled_reads_environment(self) -> None:
        with patch.dict("os.environ", {"POST_DEBUG_LAYOUT": "1"}):
            self.assertTrue(enabled())
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(enabled())

    def test_probe_registered_reports_invalid_measure(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_name("probe-test-box")

        with patch.object(
            Gtk.Box,
            "measure",
            return_value=(250, 200, -1, -1),
        ):
            with patch("post.layout_debug.enabled", return_value=True):
                clear_registry()
                name_widget(box, "probe-test-box")
                with self.assertLogs("post.layout_debug", level="WARNING") as captured:
                    bad = probe_registered(context="unit-test")

        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0][0].startswith("probe-test-box@"))
        self.assertIn("min width 250 natural width 200", captured.output[0])


if __name__ == "__main__":
    unittest.main()
