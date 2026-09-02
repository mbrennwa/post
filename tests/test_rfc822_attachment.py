# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Attached RFC 822 messages (#385)."""

from __future__ import annotations

import email
import email.policy
import unittest
from email.mime.application import MIMEApplication
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

from post.mail.helpers import (
    attachment_payload_bytes,
    extract_attachments_from_email_message,
    get_attachment_data_from_rfc822_bytes,
    looks_like_rfc822_attachment,
    message_dict_from_rfc822_bytes,
    rfc822_attachment_filename,
    subject_from_rfc822_bytes,
)


def _parse(msg: email.message.Message) -> email.message.EmailMessage:
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


def _inner_text_message() -> MIMEText:
    inner = MIMEText("Please see the ETA discussion.")
    inner["Subject"] = "Re: Pulsar miniRuedi ETA?"
    inner["From"] = "dave@example.com"
    inner["To"] = "you@example.com"
    inner["Date"] = "Fri, 31 Jul 2026 17:27:09 +0000"
    return inner


def _outer_with_attached_email(*, with_inner_pdf: bool = False) -> email.message.Message:
    if with_inner_pdf:
        inner: email.message.Message = MIMEMultipart("mixed")
        inner["Subject"] = "FW: BN waybill"
        inner["From"] = "dan@example.com"
        inner["To"] = "nick@example.com"
        inner["Date"] = "Tue, 25 Aug 2026 15:09:10 +0000"
        inner.attach(MIMEText("DHL statement attached."))
        pdf = MIMEApplication(b"%PDF-fake-waybill", _subtype="pdf")
        pdf.add_header("Content-Disposition", "attachment", filename="waybill.pdf")
        inner.attach(pdf)
    else:
        inner = _inner_text_message()

    outer = MIMEMultipart("mixed")
    outer["Subject"] = "RE: Shipment"
    outer["From"] = "nick@example.com"
    outer["To"] = "you@example.com"
    outer.attach(MIMEText("see his attached email"))
    rfc = MIMEMessage(inner)
    rfc.add_header("Content-Disposition", "attachment")
    outer.attach(rfc)
    return _parse(outer)


class LooksLikeRfc822Tests(unittest.TestCase):
    def test_message_rfc822_mime(self) -> None:
        self.assertTrue(looks_like_rfc822_attachment("message/rfc822", None, None))
        self.assertTrue(
            looks_like_rfc822_attachment("message/global; charset=utf-8", None, None)
        )

    def test_eml_filename(self) -> None:
        self.assertTrue(looks_like_rfc822_attachment("application/octet-stream", "note.eml"))

    def test_sniff_headers(self) -> None:
        data = b"From: a@example.com\nSubject: Hi\n\nBody\n"
        self.assertTrue(looks_like_rfc822_attachment("attachment", "attachment", data))

    def test_rejects_pdf_and_ole(self) -> None:
        self.assertFalse(looks_like_rfc822_attachment(None, "doc.pdf", b"%PDF-1.4"))
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
        self.assertFalse(looks_like_rfc822_attachment(None, "file.msg", ole))


class Rfc822FilenameTests(unittest.TestCase):
    def test_uses_subject(self) -> None:
        self.assertEqual(
            rfc822_attachment_filename("Re: Pulsar miniRuedi ETA?"),
            "Re: Pulsar miniRuedi ETA?.eml",
        )

    def test_sanitizes_path_chars(self) -> None:
        self.assertEqual(rfc822_attachment_filename("a/b\\c"), "a_b_c.eml")

    def test_fallback_when_empty(self) -> None:
        self.assertEqual(rfc822_attachment_filename(None), "Forwarded message.eml")
        self.assertEqual(rfc822_attachment_filename("   "), "Forwarded message.eml")


class Rfc822LeafAttachmentTests(unittest.TestCase):
    def test_parent_lists_rfc822_not_inner_pdf(self) -> None:
        msg = _outer_with_attached_email(with_inner_pdf=True)
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["mime_type"], "message/rfc822")
        self.assertEqual(attachments[0]["filename"], "FW: BN waybill.eml")
        names = [a["filename"] for a in attachments]
        self.assertNotIn("waybill.pdf", names)

    def test_nameless_rfc822_uses_nested_subject(self) -> None:
        msg = _outer_with_attached_email()
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(
            attachments[0]["filename"],
            "Re: Pulsar miniRuedi ETA?.eml",
        )

    def test_payload_bytes_are_parseable_rfc822(self) -> None:
        msg = _outer_with_attached_email()
        parts = [
            part
            for part in msg.walk()
            if part.get_content_type() == "message/rfc822"
        ]
        self.assertEqual(len(parts), 1)
        payload = attachment_payload_bytes(parts[0])
        self.assertTrue(payload)
        self.assertIn(b"Re: Pulsar miniRuedi ETA?", payload)
        self.assertEqual(
            subject_from_rfc822_bytes(payload),
            "Re: Pulsar miniRuedi ETA?",
        )

    def test_nested_message_dict_lists_inner_pdf(self) -> None:
        outer = _outer_with_attached_email(with_inner_pdf=True)
        raw, data = get_attachment_data_from_rfc822_bytes(outer.as_bytes(), 0)
        self.assertTrue(str(raw).endswith(".eml"))
        parsed = message_dict_from_rfc822_bytes(data)
        self.assertEqual(parsed["subject"], "FW: BN waybill")
        self.assertEqual(parsed["from"], "dan@example.com")
        names = [a["filename"] for a in parsed["attachments"]]
        self.assertEqual(names, ["waybill.pdf"])
        inner_name, inner_data = get_attachment_data_from_rfc822_bytes(data, 0)
        self.assertEqual(inner_name, "waybill.pdf")
        self.assertEqual(inner_data, b"%PDF-fake-waybill")

    def test_message_dict_headers_and_body(self) -> None:
        inner = _inner_text_message()
        parsed = message_dict_from_rfc822_bytes(inner.as_bytes())
        self.assertEqual(parsed["subject"], "Re: Pulsar miniRuedi ETA?")
        self.assertEqual(parsed["from"], "dave@example.com")
        self.assertIn("ETA discussion", parsed["body_plain"] or "")
        self.assertEqual(parsed["attachments"], [])


class OpenAttachmentRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        if not Gtk.is_initialized():
            Gtk.init()

    def test_rfc822_opens_nested_window(self) -> None:
        from post.attachment_open import open_attachment

        parent = MagicMock()
        data = b"From: a@example.com\nSubject: Hi\n\nBody\n"
        with patch(
            "post.attached_message_window.present_attached_message"
        ) as present:
            open_attachment(
                parent,
                filename="attachment",
                data=data,
                mime_type="message/rfc822",
            )
        present.assert_called_once()
        kwargs = present.call_args.kwargs
        self.assertEqual(kwargs["data"], data)

    def test_pdf_uses_desktop(self) -> None:
        from post.attachment_open import open_attachment

        parent = MagicMock()
        with (
            patch("post.attachment_open.write_temp_attachment", return_value="/tmp/x.pdf"),
            patch("post.attachment_open.Gio") as gio,
        ):
            gio.File.new_for_path.return_value.get_uri.return_value = "file:///tmp/x.pdf"
            gio.AppInfo.launch_default_for_uri.return_value = True
            open_attachment(
                parent,
                filename="waybill.pdf",
                data=b"%PDF-fake",
                mime_type="application/pdf",
            )
        gio.AppInfo.launch_default_for_uri.assert_called_once()


if __name__ == "__main__":
    unittest.main()
