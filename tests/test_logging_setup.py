# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from post import logging_setup


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._state = Path(self._tmpdir.name) / "state"
        self._env = mock.patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self._state)},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        # Ensure POST_LOG_LEVEL does not leak from the developer shell.
        os.environ.pop("POST_LOG_LEVEL", None)
        logging_setup._reset_for_tests()
        self.addCleanup(logging_setup._reset_for_tests)
        self.addCleanup(self._restore_root_level)
        self._prev_level = logging.root.level

    def _restore_root_level(self) -> None:
        logging.root.setLevel(self._prev_level)

    def test_log_path_respects_xdg_state_home(self) -> None:
        self.assertEqual(
            logging_setup.log_file_path(),
            self._state / "post" / "post.log",
        )

    def test_configure_logging_is_idempotent(self) -> None:
        path1 = logging_setup.configure_logging()
        handlers_after_first = [
            h
            for h in logging.root.handlers
            if getattr(h, "name", None)
            in {
                logging_setup._FILE_HANDLER_NAME,
                logging_setup._STREAM_HANDLER_NAME,
            }
        ]
        path2 = logging_setup.configure_logging()
        handlers_after_second = [
            h
            for h in logging.root.handlers
            if getattr(h, "name", None)
            in {
                logging_setup._FILE_HANDLER_NAME,
                logging_setup._STREAM_HANDLER_NAME,
            }
        ]
        self.assertEqual(path1, path2)
        self.assertEqual(len(handlers_after_first), 2)
        self.assertEqual(len(handlers_after_second), 2)

    def test_info_goes_to_file_warning_to_stream(self) -> None:
        stream = io.StringIO()
        path = logging_setup.configure_logging()
        # Replace stream handler stream so we can assert without polluting pytest.
        for handler in logging.root.handlers:
            if getattr(handler, "name", None) == logging_setup._STREAM_HANDLER_NAME:
                handler.setStream(stream)
        logger = logging.getLogger("post.test.logging")
        logger.debug("debug-only-event")
        logger.info("info-only-event")
        logger.warning("warning-event")
        for handler in logging.root.handlers:
            handler.flush()

        text = path.read_text(encoding="utf-8")
        self.assertIn("debug-only-event", text)
        self.assertIn("info-only-event", text)
        self.assertIn("warning-event", text)
        stream_text = stream.getvalue()
        self.assertNotIn("debug-only-event", stream_text)
        self.assertNotIn("info-only-event", stream_text)
        self.assertIn("warning-event", stream_text)

    def test_default_debug_goes_to_file(self) -> None:
        path = logging_setup.configure_logging()
        logger = logging.getLogger("post.test.logging")
        logger.debug("debug-event")
        for handler in logging.root.handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        self.assertIn("debug-event", text)

    def test_post_log_level_warning_raises_file_threshold(self) -> None:
        with mock.patch.dict(os.environ, {"POST_LOG_LEVEL": "WARNING"}):
            logging_setup._reset_for_tests()
            path = logging_setup.configure_logging()
            logger = logging.getLogger("post.test.logging")
            logger.debug("debug-event")
            logger.warning("warning-event")
            for handler in logging.root.handlers:
                handler.flush()
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("debug-event", text)
            self.assertIn("warning-event", text)

    def test_open_log_file_uri(self) -> None:
        uri = logging_setup.open_log_file_uri()
        self.assertTrue(uri.startswith("file://"))
        self.assertIn("post.log", uri)


if __name__ == "__main__":
    unittest.main()
