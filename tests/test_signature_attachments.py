# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Hide S/MIME and OpenPGP protocol signatures from the attachment list (#417)."""

from __future__ import annotations

import email
import email.policy
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock

from post.mail.calendar_invite import email_part_counts_as_attachment
from post.mail.helpers import (
    extract_attachments_from_email_message,
    has_visible_attachments,
    looks_like_crypto_signature_part,
    message_dict_from_rfc822_bytes,
    message_row_is_signature_suspect,
    skip_multipart_signed_signature,
    _mime_part_is_attachment,
)


def _parse(msg: email.message.Message) -> email.message.EmailMessage:
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


def _pkcs7_signature() -> MIMEApplication:
    part = MIMEApplication(b"pkcs7-sig", _subtype="pkcs7-signature", name="smime.p7s")
    part.add_header("Content-Disposition", "attachment", filename="smime.p7s")
    return part


def _pgp_signature() -> MIMEApplication:
    part = MIMEApplication(
        b"-----BEGIN PGP SIGNATURE-----\n",
        _subtype="pgp-signature",
        name="signature.asc",
    )
    part.add_header("Content-Disposition", "attachment", filename="signature.asc")
    return part


def _pdf_attachment() -> MIMEApplication:
    part = MIMEApplication(b"%PDF-fake", _subtype="pdf", name="file.pdf")
    part.add_header("Content-Disposition", "attachment", filename="file.pdf")
    return part


def _signed_message(*parts: email.message.Message) -> email.message.EmailMessage:
    outer = MIMEMultipart("signed", protocol="application/pkcs7-signature")
    if len(parts) == 1:
        outer.attach(parts[0])
    else:
        inner = MIMEMultipart("mixed")
        for part in parts:
            inner.attach(part)
        outer.attach(inner)
    outer.attach(_pkcs7_signature())
    return _parse(outer)


class CryptoSignatureHelperTests(unittest.TestCase):
    def test_looks_like_smime_and_openpgp(self) -> None:
        self.assertTrue(
            looks_like_crypto_signature_part("application/pkcs7-signature", None)
        )
        self.assertTrue(
            looks_like_crypto_signature_part(
                "application/x-pkcs7-signature", "smime.p7s"
            )
        )
        self.assertTrue(
            looks_like_crypto_signature_part("application/pgp-signature", None)
        )
        self.assertTrue(
            looks_like_crypto_signature_part("application/octet-stream", "signature.asc")
        )
        self.assertFalse(
            looks_like_crypto_signature_part("application/pkcs7-mime", "smime.p7m")
        )
        self.assertFalse(looks_like_crypto_signature_part("application/pdf", "file.pdf"))

    def test_skip_only_under_multipart_signed(self) -> None:
        self.assertTrue(
            skip_multipart_signed_signature(
                "application/pkcs7-signature",
                "smime.p7s",
                "multipart/signed",
            )
        )
        self.assertFalse(
            skip_multipart_signed_signature(
                "application/pkcs7-signature",
                "smime.p7s",
                "multipart/mixed",
            )
        )
        self.assertFalse(
            skip_multipart_signed_signature(
                "application/pdf",
                "file.pdf",
                "multipart/signed",
            )
        )

    def test_has_visible_attachments(self) -> None:
        self.assertFalse(has_visible_attachments([]))
        self.assertTrue(
            has_visible_attachments(
                [{"filename": "file.pdf", "mime_type": "application/pdf"}]
            )
        )

    def test_signature_suspect_needs_attachments_and_secure_or_signed_root(self) -> None:
        self.assertFalse(
            message_row_is_signature_suspect({"flags": {"attachments": False}})
        )
        self.assertFalse(
            message_row_is_signature_suspect({"flags": {"attachments": True}})
        )
        self.assertTrue(
            message_row_is_signature_suspect(
                {"flags": {"attachments": True, "secure": True}}
            )
        )
        self.assertTrue(
            message_row_is_signature_suspect(
                {
                    "flags": {"attachments": True},
                    "content_type": "multipart/signed; protocol=application/pkcs7-signature",
                }
            )
        )
        self.assertFalse(
            message_row_is_signature_suspect(
                {
                    "flags": {"attachments": True},
                    "content_type": "multipart/mixed",
                }
            )
        )


class ExtractSignedAttachmentsTests(unittest.TestCase):
    def test_signed_only_lists_no_attachments(self) -> None:
        msg = _signed_message(MIMEText("hello"))
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual(attachments, [])
        parsed = message_dict_from_rfc822_bytes(msg.as_bytes())
        self.assertFalse(parsed["flags"]["attachments"])

    def test_signed_plus_pdf_keeps_only_the_file(self) -> None:
        msg = _signed_message(MIMEText("see pdf"), _pdf_attachment())
        attachments = extract_attachments_from_email_message(msg)
        self.assertEqual([item["filename"] for item in attachments], ["file.pdf"])
        parsed = message_dict_from_rfc822_bytes(msg.as_bytes())
        self.assertTrue(parsed["flags"]["attachments"])

    def test_openpgp_signature_under_signed_is_dropped(self) -> None:
        outer = MIMEMultipart("signed", protocol="application/pgp-signature")
        outer.attach(MIMEText("hello"))
        outer.attach(_pgp_signature())
        attachments = extract_attachments_from_email_message(_parse(outer))
        self.assertEqual(attachments, [])

    def test_leftover_p7s_under_mixed_is_kept(self) -> None:
        mixed = MIMEMultipart("mixed")
        mixed.attach(MIMEText("hello"))
        mixed.attach(_pkcs7_signature())
        attachments = extract_attachments_from_email_message(_parse(mixed))
        self.assertEqual([item["filename"] for item in attachments], ["smime.p7s"])

    def test_pkcs7_mime_envelope_is_listed(self) -> None:
        mixed = MIMEMultipart("mixed")
        mixed.attach(MIMEText("encrypted payload attached"))
        envelope = MIMEApplication(
            b"cms-bytes", _subtype="pkcs7-mime", name="smime.p7m"
        )
        envelope.add_header("Content-Disposition", "attachment", filename="smime.p7m")
        mixed.attach(envelope)
        attachments = extract_attachments_from_email_message(_parse(mixed))
        self.assertEqual([item["filename"] for item in attachments], ["smime.p7m"])

    def test_email_part_counts_uses_parent_type(self) -> None:
        sig = _parse(_pkcs7_signature())
        self.assertFalse(
            email_part_counts_as_attachment(sig, parent_ctype="multipart/signed")
        )
        self.assertTrue(
            email_part_counts_as_attachment(sig, parent_ctype="multipart/mixed")
        )


class CamelSignaturePartTests(unittest.TestCase):
    def _signature_part(self) -> MagicMock:
        part = MagicMock()
        part.get_filename.return_value = "smime.p7s"
        part.get_disposition.return_value = "attachment"
        part.get_content_id.return_value = None
        content_type = MagicMock()
        content_type.simple.return_value = "application/pkcs7-signature"
        part.get_content_type.return_value = content_type
        disposition = MagicMock()
        disposition.is_attachment.return_value = True
        disposition.is_attachment_ex.return_value = False
        part.get_content_disposition.return_value = disposition
        return part

    def test_camel_skips_p7s_under_signed(self) -> None:
        parent = MagicMock()
        parent.simple.return_value = "multipart/signed"
        self.assertFalse(
            _mime_part_is_attachment(
                self._signature_part(),
                "application/pkcs7-signature",
                parent_content_type=parent,
            )
        )

    def test_camel_keeps_p7s_under_mixed(self) -> None:
        parent = MagicMock()
        parent.simple.return_value = "multipart/mixed"
        self.assertTrue(
            _mime_part_is_attachment(
                self._signature_part(),
                "application/pkcs7-signature",
                parent_content_type=parent,
            )
        )


if __name__ == "__main__":
    unittest.main()
