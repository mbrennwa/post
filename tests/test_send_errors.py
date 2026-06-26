# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from gi.repository import GLib

from post.mail.send_errors import (
    SendError,
    SYSTEM_MAIL_EXTERNAL_RECIPIENTS,
    is_compose_validation_error,
    user_send_error_message,
)


class SendErrorMessageTests(unittest.TestCase):
    def test_send_error_uses_user_message(self) -> None:
        exc = SendError("Please try again later.")
        self.assertEqual(user_send_error_message(exc), "Please try again later.")

    def test_localhost_connection_refused(self) -> None:
        exc = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"),
            "Could not connect to 127.0.0.1: Connection refused",
            39,
        )
        message = user_send_error_message(exc)
        self.assertIn("No mail server is running", message)
        self.assertNotIn("127.0.0.1", message)

    def test_runtime_error_wrapper(self) -> None:
        exc = RuntimeError(
            "Could not send message: Could not connect to mail.example.com: Connection refused"
        )
        message = user_send_error_message(exc)
        self.assertIn("Could not reach the mail server", message)

    def test_timeout(self) -> None:
        self.assertIn(
            "too long",
            user_send_error_message(TimeoutError()),
        )

    def test_system_mail_external_message_is_preserved(self) -> None:
        exc = SendError(SYSTEM_MAIL_EXTERNAL_RECIPIENTS)
        self.assertIn("System mail can only send", user_send_error_message(exc))

    def test_invalid_address_value_error(self) -> None:
        exc = ValueError('The address "@xyz" is not valid.')
        self.assertEqual(user_send_error_message(exc), 'The address "@xyz" is not valid.')

    def test_empty_to_value_error(self) -> None:
        exc = ValueError("At least one To address is required")
        self.assertEqual(
            user_send_error_message(exc),
            "Add a recipient in the To field.",
        )

    def test_header_line_break_value_error(self) -> None:
        exc = ValueError("Subject must not contain line breaks.")
        self.assertEqual(user_send_error_message(exc), "Subject must not contain line breaks.")

    def test_is_compose_validation_error(self) -> None:
        self.assertTrue(
            is_compose_validation_error(ValueError("Subject must not contain line breaks."))
        )
        self.assertTrue(
            is_compose_validation_error(SendError("Subject must not contain line breaks."))
        )
        self.assertFalse(is_compose_validation_error(SendError("SMTP failed")))
        self.assertFalse(is_compose_validation_error(TimeoutError()))

    def test_send_queued_message(self) -> None:
        from post.mail.send_errors import MESSAGE_QUEUED, SendQueued

        exc = SendQueued(MESSAGE_QUEUED)
        self.assertEqual(user_send_error_message(exc), MESSAGE_QUEUED)
