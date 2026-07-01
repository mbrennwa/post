# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from post.mail.helpers import write_temp_attachment


class WriteTempAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._patch = mock.patch(
            "gi.repository.GLib.get_tmp_dir", return_value=self._tmpdir
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_writes_file(self) -> None:
        data = b"hello attachment"
        path = write_temp_attachment("report.pdf", data)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_sanitizes_path_separators(self) -> None:
        path = write_temp_attachment("../../etc/passwd", b"x")
        self.assertEqual(os.path.basename(path), ".._.._etc_passwd")
        self.assertTrue(path.startswith(os.path.join(self._tmpdir, "post")))

    def test_avoids_collision(self) -> None:
        first = write_temp_attachment("photo.png", b"first")
        second = write_temp_attachment("photo.png", b"second")
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.basename(second), "photo-1.png")
        with open(first, "rb") as handle:
            self.assertEqual(handle.read(), b"first")
        with open(second, "rb") as handle:
            self.assertEqual(handle.read(), b"second")


if __name__ == "__main__":
    unittest.main()
