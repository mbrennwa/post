# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.compose import build_plain_mime_message
from post.mail.helpers import (
    _decode_text_bytes,
    _decode_text_part,
    extract_message_bodies,
)


class _FakeMimeMessage:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def write_to_stream_sync(self, stream, _cancellable) -> bool:
        stream.write(self._raw)
        return True


class DecodeTextBytesTests(unittest.TestCase):
    def test_declared_charset(self) -> None:
        raw = "Café".encode("iso-8859-1")
        self.assertEqual(_decode_text_bytes(raw, "iso-8859-1"), "Café")

    def test_fallback_to_latin_1(self) -> None:
        raw = b"\xe9"
        self.assertEqual(_decode_text_bytes(raw, None), "é")

    def test_unknown_charset_falls_back(self) -> None:
        raw = "Café".encode("iso-8859-1")
        self.assertEqual(_decode_text_bytes(raw, "unknown-charset-xyz"), "Café")


class DecodeTextPartTests(unittest.TestCase):
    def test_iso_8859_1_plain_part(self) -> None:
        part = Camel.MimePart.new()
        part.set_content("Gr\xfc\xdf".encode("latin-1"), "text/plain; charset=iso-8859-1")
        self.assertEqual(_decode_text_part(part), "Grüß")

    def test_windows_1252_html_part(self) -> None:
        part = Camel.MimePart.new()
        part.set_content(
            b"<p>Hello \x96 world</p>",
            "text/html; charset=windows-1252",
        )
        self.assertEqual(_decode_text_part(part), "<p>Hello \u2013 world</p>")


class ExtractMessageBodiesTests(unittest.TestCase):
    def test_utf8_roundtrip(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Café",
        )
        bodies = extract_message_bodies(message)
        self.assertEqual(bodies["plain"], "Café")

    def test_email_fallback_decodes_iso_8859_1(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Latin-1 test
MIME-Version: 1.0
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: base64

R3L83wo=
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertEqual(bodies["plain"], "Grüß\n")

    def test_email_fallback_decodes_multipart_charsets(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Mixed charset
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="bound"

--bound
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: base64

Q2Fm6Qo=
--bound
Content-Type: text/html; charset=windows-1252
Content-Transfer-Encoding: base64

PHA+SGVsbG8gljwvcD4K
--bound--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertEqual(bodies["plain"], "Café\n")
        self.assertEqual(bodies["html"], "<p>Hello \u2013</p>\n")


if __name__ == "__main__":
    unittest.main()
