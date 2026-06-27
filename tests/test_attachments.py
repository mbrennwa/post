# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.helpers import extract_inline_images, extract_attachments, get_attachment_data


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

_SAMPLE_INLINE_MIME = b"""From: a@example.com
To: b@example.com
Subject: Inline image test
MIME-Version: 1.0
Content-Type: multipart/related; boundary="bound"

--bound
Content-Type: text/html; charset=utf-8

<html><body><img src="cid:logo@local"></body></html>
--bound
Content-Type: image/png
Content-Transfer-Encoding: base64
Content-ID: <logo@local>
Content-Disposition: inline; filename="logo.png"

iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADULEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==
--bound--
"""

_SAMPLE_RFC5987_MIME = b"""From: a@example.com
To: b@example.com
Subject: Unicode attachment
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="bound"

--bound
Content-Type: text/plain

Hello
--bound
Content-Type: application/pdf
Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf

%PDF-fake-content
--bound--
"""

_SAMPLE_RFC2047_FILENAME_MIME = b"""From: a@example.com
To: b@example.com
Subject: Encoded-word attachment
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="bound"

--bound
Content-Type: text/plain

Hello
--bound
Content-Type: application/pdf
Content-Disposition: attachment; filename="=?ISO-8859-1?Q?r=E9sum=E9.pdf?="

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

    def test_email_fallback_decodes_rfc5987_filename(self) -> None:
        mime = _FakeMimeMessage(_SAMPLE_RFC5987_MIME)
        attachments = extract_attachments(mime)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "résumé.pdf")
        filename, data = get_attachment_data(mime, 0)
        self.assertEqual(filename, "résumé.pdf")
        self.assertIn(b"%PDF-fake-content", data)

    def test_email_fallback_decodes_rfc2047_filename(self) -> None:
        mime = _FakeMimeMessage(_SAMPLE_RFC2047_FILENAME_MIME)
        attachments = extract_attachments(mime)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "résumé.pdf")


class ExtractInlineImagesTests(unittest.TestCase):
    def test_email_fallback_extracts_inline_image_by_content_id(self) -> None:
        mime = _FakeMimeMessage(_SAMPLE_INLINE_MIME)
        images = extract_inline_images(mime)
        self.assertIn("logo@local", images)
        mime_type, data = images["logo@local"]
        self.assertEqual(mime_type, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
