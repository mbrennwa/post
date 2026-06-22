# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.smtp_send import (
    SmtpTransportConfig,
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
        )
        smtp.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
