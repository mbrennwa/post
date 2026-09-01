# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.compose import ComposeAttachment, build_plain_mime_message
from post.mail.helpers import (
    _decode_text_bytes,
    _decode_text_part,
    extract_attachments,
    extract_message_bodies,
    extract_message_bodies_from_bytes,
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

    def test_attached_html_does_not_replace_plain_reply(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Reply
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="bound"

--bound
Content-Type: text/plain; charset="utf-8"

Hello Bob,

This is the new reply.

-----Original Message-----
From: Bob
Hello Alice
--bound
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<div style="white-space:pre-wrap">Hello Alice</div>
--bound--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIn("This is the new reply", bodies["plain"] or "")
        self.assertIsNone(bodies["html"])
        attachments = extract_attachments(mime)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "ATT00001.htm")

    def test_inline_html_alternative_still_preferred(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: HTML mail
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="bound"

--bound
Content-Type: text/plain; charset="utf-8"

Plain reply
--bound
Content-Type: text/html; charset="utf-8"

<p>HTML reply</p>
--bound--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertEqual((bodies["plain"] or "").strip(), "Plain reply")
        self.assertEqual((bodies["html"] or "").strip(), "<p>HTML reply</p>")

    def test_attached_html_used_when_no_other_body(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: HTML only
MIME-Version: 1.0
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<p>Only html</p>
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIsNone(bodies["plain"])
        self.assertIn("Only html", bodies["html"] or "")

    def test_empty_inline_alternative_promotes_attached_html(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Fwd: intro
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset="utf-8"


--alt
Content-Type: text/html; charset="utf-8"


--alt--
--mixed
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<p>Forwarded HTML</p>
--mixed--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIsNone(bodies["plain"])
        self.assertIn("Forwarded HTML", bodies["html"] or "")
        from_bytes = extract_message_bodies_from_bytes(raw)
        self.assertEqual(from_bytes["html"], bodies["html"])
        self.assertIsNone(from_bytes["plain"])

    def test_stub_inline_html_promotes_attached_html(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Fwd: intro
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: text/html; charset="utf-8"

<html><body></body></html>
--mixed
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<p>Forwarded HTML</p>
--mixed--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIsNone(bodies["plain"])
        self.assertIn("Forwarded HTML", bodies["html"] or "")
        self.assertNotIn("<html>", bodies["html"] or "")

    def test_empty_inline_html_keeps_real_plain_reply(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Reply
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="bound"

--bound
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset="utf-8"

Hello Bob, this is the new reply.
--alt
Content-Type: text/html; charset="utf-8"

<html><body></body></html>
--alt--
--bound
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<div>Quoted original</div>
--bound--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIn("this is the new reply", bodies["plain"] or "")
        self.assertIsNone(bodies["html"])

    def test_image_only_inline_html_is_not_a_stub(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Photo
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: text/html; charset="utf-8"

<img src="cid:photo@example">
--mixed
Content-Type: text/html; name="ATT00001.htm"
Content-Disposition: attachment; filename="ATT00001.htm"

<div>Quoted original</div>
--mixed--
"""
        mime = _FakeMimeMessage(raw)
        bodies = extract_message_bodies(mime)
        self.assertIn("cid:photo@example", bodies["html"] or "")
        self.assertNotIn("Quoted original", bodies["html"] or "")

    def test_camel_walk_skips_attached_html_when_plain_exists(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Reply",
            body="Hello Bob,\n\nThis is the new reply.",
            attachments=[
                ComposeAttachment(
                    filename="ATT00001.htm",
                    mime_type="text/html",
                    data=b'<div style="white-space:pre-wrap">Hello Alice</div>',
                )
            ],
        )
        bodies = extract_message_bodies(message)
        self.assertIn("This is the new reply", bodies["plain"] or "")
        self.assertIsNone(bodies["html"])
        attachments = extract_attachments(message)
        self.assertEqual(attachments[0]["filename"], "ATT00001.htm")

    def test_camel_inline_html_wins_over_attached_html(self) -> None:
        message = build_plain_mime_message(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Reply",
            body="Plain reply",
            body_html="<p>HTML reply</p>",
            attachments=[
                ComposeAttachment(
                    filename="ATT00001.htm",
                    mime_type="text/html",
                    data=b"<div>Quoted original</div>",
                )
            ],
        )
        bodies = extract_message_bodies(message)
        self.assertIn("HTML reply", bodies["html"] or "")
        self.assertNotIn("Quoted original", bodies["html"] or "")


if __name__ == "__main__":
    unittest.main()
