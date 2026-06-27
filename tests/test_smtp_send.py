# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.compose import build_outbound_email_bytes
from post.mail.smtp_send import (
    SmtpTransportConfig,
    _payload_has_8bit_parts,
    _prepare_smtp_payload,
    _reencode_8bit_parts_for_7bit_smtp,
    _recipient_addresses,
    read_smtp_transport_config,
    send_via_smtp,
)


class RecipientAddressTests(unittest.TestCase):
    def test_collects_to_cc_bcc(self) -> None:
        addresses = _recipient_addresses(
            ["Alice <alice@example.com>"],
            ["bob@example.com"],
            ["Bcc <bcc@example.com>"],
        )
        self.assertEqual(
            addresses,
            ["alice@example.com", "bob@example.com", "bcc@example.com"],
        )

    def test_email_as_display_name(self) -> None:
        addresses = _recipient_addresses(
            ["mbrennwa@gmail.com <mbrennwa@gmail.com>"],
            None,
            None,
        )
        self.assertEqual(addresses, ["mbrennwa@gmail.com"])


class ReadSmtpTransportConfigTests(unittest.TestCase):
    def test_reads_host_port_and_security(self) -> None:
        registry = mock.Mock()
        source = mock.Mock()
        registry.ref_source.return_value = source
        auth = mock.Mock()
        auth.get_host.return_value = "smtp.example.com"
        auth.get_port.return_value = 465
        auth.get_user.return_value = "user@example.com"
        auth.get_method.return_value = "PLAIN"
        security = mock.Mock()
        security.get_method.return_value = "ssl-on-alternate-port"
        source.get_extension.side_effect = lambda name: {
            "Authentication": auth,
            "Security": security,
        }[name]
        source.has_extension.return_value = True

        transport_source, config = read_smtp_transport_config(registry, "transport-1")

        self.assertIs(transport_source, source)
        self.assertEqual(
            config,
            SmtpTransportConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                security="ssl-on-alternate-port",
                auth_method="plain",
            ),
        )


class ConnectSmtpTests(unittest.TestCase):
    @mock.patch("post.mail.smtp_send.smtplib.SMTP_SSL")
    def test_ssl_connection_issues_ehlo(self, smtp_ssl_cls: mock.Mock) -> None:
        smtp = mock.Mock()
        smtp_ssl_cls.return_value = smtp
        config = SmtpTransportConfig(
            host="smtp.gmail.com",
            port=465,
            username="user@example.com",
            security="ssl-on-alternate-port",
            auth_method="xoauth2",
        )

        from post.mail.smtp_send import _connect_smtp

        result = _connect_smtp(config)

        self.assertIs(result, smtp)
        smtp.ehlo.assert_called_once()


class SendViaSmtpTests(unittest.TestCase):
    @mock.patch("post.mail.smtp_send._authenticate_smtp")
    @mock.patch("post.mail.smtp_send._connect_smtp")
    @mock.patch("post.mail.smtp_send.read_smtp_transport_config")
    def test_sendmail_called_with_recipients(
        self,
        read_config,
        connect_smtp,
        _authenticate,
    ) -> None:
        transport_source = mock.Mock()
        read_config.return_value = (
            transport_source,
            SmtpTransportConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                security="ssl-on-alternate-port",
                auth_method="plain",
            ),
        )
        smtp = mock.Mock()
        connect_smtp.return_value = smtp
        registry = mock.Mock()

        send_via_smtp(
            registry=registry,
            transport_uid="transport-1",
            payload=b"raw",
            envelope_from="user@example.com",
            to=["dest@example.com"],
            cc=None,
            bcc=None,
        )

        smtp.sendmail.assert_called_once_with(
            "user@example.com",
            ["dest@example.com"],
            b"raw",
            mail_options=[],
        )
        smtp.quit.assert_called_once()

    @mock.patch("post.mail.smtp_send._authenticate_smtp")
    @mock.patch("post.mail.smtp_send._connect_smtp")
    @mock.patch("post.mail.smtp_send.read_smtp_transport_config")
    def test_sendmail_includes_bcc_in_recipients(
        self,
        read_config,
        connect_smtp,
        _authenticate,
    ) -> None:
        transport_source = mock.Mock()
        read_config.return_value = (
            transport_source,
            SmtpTransportConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                security="ssl-on-alternate-port",
                auth_method="plain",
            ),
        )
        smtp = mock.Mock()
        connect_smtp.return_value = smtp
        registry = mock.Mock()

        send_via_smtp(
            registry=registry,
            transport_uid="transport-1",
            payload=b"raw",
            envelope_from="user@example.com",
            to=["dest@example.com"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
        )

        smtp.sendmail.assert_called_once_with(
            "user@example.com",
            ["dest@example.com", "cc@example.com", "bcc@example.com"],
            b"raw",
            mail_options=[],
        )


class EightBitMimeTests(unittest.TestCase):
    def test_payload_has_8bit_parts_detects_non_ascii_wire_bytes(self) -> None:
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )
        self.assertTrue(_payload_has_8bit_parts(payload))

    def test_payload_has_8bit_parts_false_for_ascii(self) -> None:
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="ASCII",
            body="Hello",
        )
        self.assertFalse(_payload_has_8bit_parts(payload))

    def test_reencode_8bit_parts_uses_quoted_printable(self) -> None:
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )
        reencoded = _reencode_8bit_parts_for_7bit_smtp(payload)
        self.assertNotIn(b"Content-Transfer-Encoding: 8bit", reencoded)
        self.assertIn(b"Content-Transfer-Encoding: quoted-printable", reencoded)
        self.assertIn(b"Caf=C3=A9", reencoded)

    def test_prepare_smtp_payload_requests_body_8bitmime(self) -> None:
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )
        smtp = mock.Mock()
        smtp.has_extn.return_value = True

        wire_payload, mail_options = _prepare_smtp_payload(smtp, payload)

        self.assertIs(wire_payload, payload)
        self.assertEqual(mail_options, ["BODY=8BITMIME"])
        smtp.has_extn.assert_called_once_with("8bitmime")

    def test_prepare_smtp_payload_reencodes_without_8bitmime(self) -> None:
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="alice@example.com",
            to=["bob@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )
        smtp = mock.Mock()
        smtp.has_extn.return_value = False

        wire_payload, mail_options = _prepare_smtp_payload(smtp, payload)

        self.assertIsNot(wire_payload, payload)
        self.assertNotIn(b"Content-Transfer-Encoding: 8bit", wire_payload)
        self.assertEqual(mail_options, [])

    @mock.patch("post.mail.smtp_send._authenticate_smtp")
    @mock.patch("post.mail.smtp_send._connect_smtp")
    @mock.patch("post.mail.smtp_send.read_smtp_transport_config")
    def test_sendmail_uses_body_8bitmime_for_non_ascii(
        self,
        read_config,
        connect_smtp,
        _authenticate,
    ) -> None:
        transport_source = mock.Mock()
        read_config.return_value = (
            transport_source,
            SmtpTransportConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                security="ssl-on-alternate-port",
                auth_method="plain",
            ),
        )
        smtp = mock.Mock()
        smtp.has_extn.return_value = True
        connect_smtp.return_value = smtp
        registry = mock.Mock()
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="user@example.com",
            to=["dest@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )

        send_via_smtp(
            registry=registry,
            transport_uid="transport-1",
            payload=payload,
            envelope_from="user@example.com",
            to=["dest@example.com"],
            cc=None,
            bcc=None,
        )

        smtp.sendmail.assert_called_once_with(
            "user@example.com",
            ["dest@example.com"],
            payload,
            mail_options=["BODY=8BITMIME"],
        )

    @mock.patch("post.mail.smtp_send._authenticate_smtp")
    @mock.patch("post.mail.smtp_send._connect_smtp")
    @mock.patch("post.mail.smtp_send.read_smtp_transport_config")
    def test_sendmail_reencodes_when_server_lacks_8bitmime(
        self,
        read_config,
        connect_smtp,
        _authenticate,
    ) -> None:
        transport_source = mock.Mock()
        read_config.return_value = (
            transport_source,
            SmtpTransportConfig(
                host="smtp.example.com",
                port=465,
                username="user@example.com",
                security="ssl-on-alternate-port",
                auth_method="plain",
            ),
        )
        smtp = mock.Mock()
        smtp.has_extn.return_value = False
        connect_smtp.return_value = smtp
        registry = mock.Mock()
        payload = build_outbound_email_bytes(
            from_name=None,
            from_address="user@example.com",
            to=["dest@example.com"],
            cc=None,
            bcc=None,
            subject="Unicode",
            body="Café",
        )

        send_via_smtp(
            registry=registry,
            transport_uid="transport-1",
            payload=payload,
            envelope_from="user@example.com",
            to=["dest@example.com"],
            cc=None,
            bcc=None,
        )

        sent_payload = smtp.sendmail.call_args.args[2]
        self.assertNotIn(b"Content-Transfer-Encoding: 8bit", sent_payload)
        smtp.sendmail.assert_called_once_with(
            "user@example.com",
            ["dest@example.com"],
            sent_payload,
            mail_options=[],
        )


if __name__ == "__main__":
    unittest.main()
