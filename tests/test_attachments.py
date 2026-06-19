# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import get_attachment_data


class _FakeMimeMessage:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def write_to_stream_sync(self, stream, _cancellable) -> bool:
        stream.write(self._raw)
        return True


_SAMPLE_MIME = b"""From: a@example.com
To: b@example.com
Subject: Attachment test
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="bound"

--bound
Content-Type: text/plain

Hello
--bound
Content-Type: application/pdf
Content-Disposition: attachment; filename="doc.pdf"

%PDF-fake-content
--bound--
"""


class GetAttachmentDataTests(unittest.TestCase):
    def test_email_fallback_extracts_attachment_bytes(self) -> None:
        mime = _FakeMimeMessage(_SAMPLE_MIME)
        filename, data = get_attachment_data(mime, 0)
        self.assertEqual(filename, "doc.pdf")
        self.assertIn(b"%PDF-fake-content", data)

    def test_missing_index_raises(self) -> None:
        mime = _FakeMimeMessage(_SAMPLE_MIME)
        with self.assertRaises(ValueError):
            get_attachment_data(mime, 99)


if __name__ == "__main__":
    unittest.main()
