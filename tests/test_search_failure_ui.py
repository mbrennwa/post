# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""A failed search shows an error, not "No Matches" (#535)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.network_errors import (
    SEARCH_FAILED_MESSAGE,
    SEARCH_NEEDS_NEWER_EDS_MESSAGE,
    format_search_error,
)
from post.window import MainWindow


class FormatSearchErrorTests(unittest.TestCase):
    def test_generic_failure(self) -> None:
        self.assertEqual(
            format_search_error(RuntimeError("something odd")), SEARCH_FAILED_MESSAGE
        )

    def test_old_eds_names_the_requirement(self) -> None:
        error = RuntimeError(
            "Camel.Folder.search_sync is not available. Post needs "
            "evolution-data-server 3.62 or newer."
        )
        self.assertEqual(format_search_error(error), SEARCH_NEEDS_NEWER_EDS_MESSAGE)

    def test_undefined_symbol_names_the_requirement(self) -> None:
        error = AttributeError(
            "/usr/lib/libcamel-1.2.so.68: undefined symbol: camel_folder_search_x"
        )
        self.assertEqual(format_search_error(error), SEARCH_NEEDS_NEWER_EDS_MESSAGE)

    def test_message_never_dumps_raw_camel_text(self) -> None:
        text = format_search_error(RuntimeError("GDBus.Error:org.gnome.evolution:0xdead"))
        self.assertNotIn("GDBus", text)


class OnSearchFailedTests(unittest.TestCase):
    def _window(self, *, generation: int = 3, query=object()):
        window = mock.Mock()
        window._messages_load_generation = generation
        window._search_query = query
        return window

    def test_shows_error_page_status_and_toast(self) -> None:
        window = self._window()
        error = RuntimeError("search failed")

        with mock.patch("post.window.show_error_toast") as toast:
            result = MainWindow._on_search_failed(window, 3, error)

        self.assertFalse(result)  # GLib.idle_add: do not repeat
        window._message_loading_spinner.stop.assert_called_once()
        window._reset_search_progress_ui.assert_called_once()
        window._message_error_label.set_label.assert_called_once_with(
            SEARCH_FAILED_MESSAGE
        )
        window._message_stack.set_visible_child_name.assert_called_once_with("error")
        window._set_status.assert_called_once_with(SEARCH_FAILED_MESSAGE)
        toast.assert_called_once_with(window, "Search failed")
        window._release_offline_sync_for_folder_work.assert_called_once_with(3)

    def test_never_shows_the_empty_or_list_state(self) -> None:
        window = self._window()

        with mock.patch("post.window.show_error_toast"):
            MainWindow._on_search_failed(window, 3, RuntimeError("x"))

        shown = [c.args[0] for c in window._message_stack.set_visible_child_name.call_args_list]
        self.assertEqual(shown, ["error"])
        window._message_empty_label.set_label.assert_not_called()

    def test_stale_load_is_ignored(self) -> None:
        window = self._window(generation=4)

        with mock.patch("post.window.show_error_toast") as toast:
            MainWindow._on_search_failed(window, 3, RuntimeError("x"))

        window._message_stack.set_visible_child_name.assert_not_called()
        toast.assert_not_called()
        window._stop_superseded_message_loading.assert_called_once_with(3)

    def test_ignored_when_the_search_was_cleared(self) -> None:
        window = self._window(query=None)

        with mock.patch("post.window.show_error_toast") as toast:
            MainWindow._on_search_failed(window, 3, RuntimeError("x"))

        window._message_stack.set_visible_child_name.assert_not_called()
        toast.assert_not_called()

    def test_partial_failure_only_toasts(self) -> None:
        window = self._window()

        with mock.patch("post.window.show_error_toast") as toast:
            MainWindow._on_search_partial_failure(window, 3, "Search incomplete: 1 search step failed.")

        toast.assert_called_once_with(window, "Search incomplete: 1 search step failed.")
        window._message_stack.set_visible_child_name.assert_not_called()

    def test_partial_failure_from_a_stale_search_is_ignored(self) -> None:
        window = self._window(generation=9)

        with mock.patch("post.window.show_error_toast") as toast:
            MainWindow._on_search_partial_failure(window, 3, "Search incomplete")

        toast.assert_not_called()


if __name__ == "__main__":
    unittest.main()
