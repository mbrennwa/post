# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import email
import unittest

import gi

gi.require_version("Camel", "1.2")
from gi.repository import Camel

from post.mail.compose import ComposeAttachment, build_plain_mime_message
from post.mail.helpers import (
    _decode_text_bytes,
    _decode_text_part,
    extract_attachments,
    extract_attachments_from_email_message,
    extract_inline_images,
    extract_inline_images_from_email_message,
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


def _mixed_with_inline_photo(*, content_id: str | None = None) -> email.message.EmailMessage:
    """text, inline jpeg, text. The shape iPad Mail uses for a body photo."""
    msg = email.message.EmailMessage(policy=email.policy.default)
    msg["Subject"] = "Hello"
    msg.make_mixed()

    leading = email.message.EmailMessage()
    leading.set_content("Good morning")
    msg.attach(leading)

    image = email.message.EmailMessage()
    image.set_content(
        b"\xff\xd8\xff\xd9",
        maintype="image",
        subtype="jpeg",
        disposition="inline",
        filename="photo.jpg",
    )
    if content_id:
        image["Content-ID"] = f"<{content_id}>"
    msg.attach(image)

    trailing = email.message.EmailMessage()
    trailing.set_content("Sent from my tablet")
    msg.attach(trailing)
    return msg


class LooseInlineImageBodyTests(unittest.TestCase):
    def test_inline_photo_sits_between_text_parts(self) -> None:
        msg = _mixed_with_inline_photo()
        raw = msg.as_bytes()
        bodies = extract_message_bodies_from_bytes(raw)
        self.assertEqual(bodies["plain"], "Good morning\nSent from my tablet")
        html_body = bodies["html"] or ""
        self.assertLess(
            html_body.find("Good morning"),
            html_body.find("<img"),
        )
        self.assertLess(
            html_body.find("<img"),
            html_body.find("Sent from my tablet"),
        )
        self.assertIn('src="cid:post-inline-0@post"', html_body)
        images = extract_inline_images_from_email_message(msg)
        self.assertEqual(set(images), {"post-inline-0@post"})
        self.assertEqual(images["post-inline-0@post"][0], "image/jpeg")
        self.assertEqual(images["post-inline-0@post"][1], b"\xff\xd8\xff\xd9")
        self.assertEqual(extract_attachments_from_email_message(msg), [])

        wrapped = extract_message_bodies(_FakeMimeMessage(raw))
        self.assertEqual(wrapped["plain"], bodies["plain"])
        self.assertEqual(wrapped["html"], bodies["html"])

    def test_existing_content_id_is_reused(self) -> None:
        msg = _mixed_with_inline_photo(content_id="keep-me@post")
        bodies = extract_message_bodies_from_bytes(msg.as_bytes())
        self.assertIn('src="cid:keep-me@post"', bodies["html"] or "")
        self.assertNotIn("post-inline-", bodies["html"] or "")
        images = extract_inline_images_from_email_message(msg)
        self.assertIn("keep-me@post", images)

    def test_attachment_image_stays_a_file(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "File"
        msg.make_mixed()
        leading = email.message.EmailMessage()
        leading.set_content("See attached")
        msg.attach(leading)
        image = email.message.EmailMessage()
        image.set_content(
            b"\xff\xd8\xff\xd9",
            maintype="image",
            subtype="jpeg",
            disposition="attachment",
            filename="photo.jpg",
        )
        msg.attach(image)
        bodies = extract_message_bodies_from_bytes(msg.as_bytes())
        self.assertEqual((bodies["plain"] or "").strip(), "See attached")
        self.assertIsNone(bodies["html"])
        names = [
            item["filename"]
            for item in extract_attachments_from_email_message(msg)
        ]
        self.assertEqual(names, ["photo.jpg"])

    def test_image_without_disposition_stays_a_file(self) -> None:
        raw = b"""From: a@example.com
To: b@example.com
Subject: Named
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: text/plain; charset="utf-8"

Hello
--mixed
Content-Type: image/jpeg; name="photo.jpg"

/9j/wA==
--mixed--
"""
        bodies = extract_message_bodies_from_bytes(raw)
        self.assertEqual((bodies["plain"] or "").strip(), "Hello")
        self.assertIsNone(bodies["html"])
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        names = [
            item["filename"]
            for item in extract_attachments_from_email_message(msg)
        ]
        self.assertIn("photo.jpg", names)

    def test_html_body_is_not_reassembled(self) -> None:
        msg = email.message.EmailMessage(policy=email.policy.default)
        msg["Subject"] = "HTML"
        msg.make_alternative()
        plain = email.message.EmailMessage()
        plain.set_content("Plain reply")
        html = email.message.EmailMessage()
        html.set_content('<p>HTML reply</p><img src="cid:logo@local">', subtype="html")
        msg.attach(plain)
        msg.attach(html)
        logo = email.message.EmailMessage()
        logo.set_content(
            b"png",
            maintype="image",
            subtype="png",
            disposition="inline",
            filename="logo.png",
        )
        logo["Content-ID"] = "<logo@local>"
        # The logo belongs to the HTML body, so the message must stay as sent.
        related = email.message.EmailMessage()
        related.make_related()
        related.attach(msg)
        related.attach(logo)

        bodies = extract_message_bodies_from_bytes(related.as_bytes())
        self.assertIn("<p>HTML reply</p>", bodies["html"] or "")
        self.assertNotIn("<pre>", bodies["html"] or "")
        images = extract_inline_images_from_email_message(related)
        self.assertIn("logo@local", images)
        self.assertNotIn("post-inline-0@post", images)
        self.assertEqual(extract_attachments_from_email_message(related), [])

    def test_signed_wrapper_still_places_the_photo(self) -> None:
        inner = _mixed_with_inline_photo()
        outer = email.message.EmailMessage(policy=email.policy.default)
        outer["Subject"] = "Signed"
        outer.set_type("multipart/signed")
        outer.set_param("protocol", "application/pkcs7-signature")
        outer.set_param("micalg", "sha-256")
        outer.attach(inner)
        signature = email.message.EmailMessage()
        signature.set_content(
            b"sig",
            maintype="application",
            subtype="pkcs7-signature",
            filename="smime.p7s",
        )
        outer.attach(signature)

        bodies = extract_message_bodies_from_bytes(outer.as_bytes())
        self.assertIn("Good morning", bodies["plain"] or "")
        self.assertIn("Sent from my tablet", bodies["plain"] or "")
        self.assertIn('src="cid:post-inline-0@post"', bodies["html"] or "")
        self.assertEqual(extract_attachments_from_email_message(outer), [])

    def test_camel_inline_photo_matches_email_ids(self) -> None:
        mixed = Camel.Multipart.new()
        mixed.set_boundary("loose-bound")
        leading = Camel.MimePart.new()
        leading.set_content(b"Good morning\n", "text/plain; charset=utf-8")
        mixed.add_part(leading)
        image = Camel.MimePart.new()
        image.set_content(b"\xff\xd8\xff\xd9", "image/jpeg")
        image.set_disposition("inline")
        image.set_filename("photo.jpg")
        mixed.add_part(image)
        trailing = Camel.MimePart.new()
        trailing.set_content(b"Sent from my tablet\n", "text/plain; charset=utf-8")
        mixed.add_part(trailing)
        message = Camel.MimeMessage.new()
        message.set_subject("Hello")
        message.props.content = mixed
        message.set_mime_type("multipart/mixed")

        bodies = extract_message_bodies(message)
        images = extract_inline_images(message)
        self.assertEqual(bodies["plain"], "Good morning\nSent from my tablet")
        self.assertIn('src="cid:post-inline-0@post"', bodies["html"] or "")
        self.assertEqual(set(images), {"post-inline-0@post"})
        self.assertEqual(images["post-inline-0@post"][1], b"\xff\xd8\xff\xd9")
        self.assertEqual(extract_attachments(message), [])


if __name__ == "__main__":
    unittest.main()
