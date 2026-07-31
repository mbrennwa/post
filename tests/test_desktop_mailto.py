# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from pathlib import Path


_DESKTOP = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "io.github.mbrennwa.Post.desktop"
)


class DesktopMailtoRegistrationTests(unittest.TestCase):
    def test_claims_mailto_handler(self) -> None:
        text = _DESKTOP.read_text(encoding="utf-8")
        self.assertIn("MimeType=x-scheme-handler/mailto;", text)
        self.assertRegex(text, r"(?m)^Exec=post %u$")


if __name__ == "__main__":
    unittest.main()
