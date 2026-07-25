# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for account not-online badge helper (#168)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.account_status import (
    TOOLTIP_ACCOUNT_OFFLINE,
    TOOLTIP_NEEDS_SIGN_IN,
    TOOLTIP_NETWORK_OFFLINE,
    TOOLTIP_NOT_CONNECTED,
    TOOLTIP_TRANSFER_BUSY,
    TOOLTIP_TRANSFER_NOT_RESPONDING,
    account_not_online_badge,
)
from post.mail.eds import FlushSendQueueResult, MailService
from post.mail.send_errors import SendError
from post.mail.send_queue import (
    SIGN_IN_FOLDER_MESSAGE,
    TOKEN_EXPIRED_FOLDER_MESSAGE,
    format_folder_load_error,
    format_sign_in_required_log,
    log_mail_error,
)


class AccountNotOnlineBadgeTests(unittest.TestCase):
    def test_user_offline_takes_precedence(self) -> None:
        show, tip = account_not_online_badge(
            user_online=False,
            connect_health="needs_sign_in",
            network_available=True,
            remote_account=True,
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_ACCOUNT_OFFLINE)

    def test_needs_sign_in(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="needs_sign_in",
            network_available=True,
            remote_account=True,
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_NEEDS_SIGN_IN)

    def test_not_connected(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="not_connected",
            network_available=True,
            remote_account=True,
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_NOT_CONNECTED)

    def test_network_offline_for_remote(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="ok",
            network_available=False,
            remote_account=True,
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_NETWORK_OFFLINE)

    def test_healthy_hides_badge(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="ok",
            network_available=True,
            remote_account=True,
        )
        self.assertFalse(show)
        self.assertEqual(tip, "")

    def test_transfer_busy_shows_badge(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="ok",
            network_available=True,
            remote_account=True,
            transfer_state="busy",
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_TRANSFER_BUSY)

    def test_transfer_not_responding_shows_badge(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="ok",
            network_available=True,
            remote_account=True,
            transfer_state="not_responding",
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_TRANSFER_NOT_RESPONDING)

    def test_needs_sign_in_beats_transfer_busy(self) -> None:
        show, tip = account_not_online_badge(
            user_online=True,
            connect_health="needs_sign_in",
            network_available=True,
            remote_account=True,
            transfer_state="busy",
        )
        self.assertTrue(show)
        self.assertEqual(tip, TOOLTIP_NEEDS_SIGN_IN)


class FolderLoadErrorFormatTests(unittest.TestCase):
    def test_token_expired_is_user_facing(self) -> None:
        exc = RuntimeError(
            'Failed to obtain an access token for "user@example.com": '
            "AADSTS70043: The refresh token has expired"
        )
        self.assertEqual(format_folder_load_error(exc), TOKEN_EXPIRED_FOLDER_MESSAGE)

    def test_auth_failure_is_user_facing(self) -> None:
        exc = RuntimeError("Authentication failed for account")
        self.assertEqual(format_folder_load_error(exc), SIGN_IN_FOLDER_MESSAGE)

    def test_generic_error_hides_raw_details(self) -> None:
        message = format_folder_load_error(RuntimeError("g-io-error-quark: weird dump"))
        self.assertNotIn("g-io-error-quark", message)
        self.assertEqual(message, "Could not load folders for this account.")

    def test_sign_in_log_omits_aadsts_dump(self) -> None:
        exc = RuntimeError(
            'Failed to obtain an access token for "user@example.com": '
            "AADSTS70043: The refresh token has expired"
        )
        detail = format_sign_in_required_log(exc)
        self.assertNotIn("AADSTS", detail)
        self.assertNotIn("user@example.com", detail)
        self.assertIn("sign-in", detail.casefold())

    def test_log_mail_error_sign_in_is_warning_without_traceback(self) -> None:
        logger = mock.Mock()
        exc = RuntimeError(
            "Failed to refresh access token (goa-error-quark, 4): AADSTS70043"
        )
        log_mail_error(logger, "Failed to list folders for acct", exc)
        logger.warning.assert_called_once()
        args = logger.warning.call_args[0]
        self.assertEqual(args[0], "%s: %s")
        self.assertEqual(args[1], "Failed to list folders for acct")
        self.assertNotIn("AADSTS", args[2])
        logger.exception.assert_not_called()


class FlushSendQueueResultTests(unittest.TestCase):
    def test_flush_surfaces_send_error(self) -> None:
        service = MailService(registry=mock.Mock())
        queued = mock.Mock()
        queued.account_uid = "acct-1"
        queued.to = ["to@example.com"]
        queued.cc = None
        queued.bcc = None
        queued.subject = "Hi"
        queued.body = "Body"
        queued.body_html = None
        queued.in_reply_to = None
        queued.references = None

        with (
            mock.patch(
                "post.mail.eds.list_queued_outbound_messages",
                return_value=[("q1", queued)],
            ),
            mock.patch("post.mail.eds.is_outbound_ready_to_send", return_value=True),
            mock.patch("post.mail.eds.load_queued_attachments", return_value=[]),
        ):
            service._is_outbound_delivery_claimed = mock.Mock(return_value=False)
            service._begin_outbound_send = mock.Mock()
            service._end_outbound_send = mock.Mock()
            service._send_message_unlocked = mock.Mock(
                side_effect=SendError("Sign-in required")
            )
            result = service._flush_send_queue_unlocked(force=True)

        self.assertIsInstance(result, FlushSendQueueResult)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.error_message, "Sign-in required")
        self.assertEqual(result.failed_account_uid, "acct-1")

    def test_connect_health_round_trip(self) -> None:
        service = MailService(registry=mock.Mock())
        seen: list[str] = []
        service.set_account_health_changed_callback(
            lambda uid: seen.append(uid) or False
        )
        with mock.patch("post.mail.eds.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)):
            service.set_account_connect_health("acct-1", "needs_sign_in")
            self.assertEqual(service.get_account_connect_health("acct-1"), "needs_sign_in")
            service.set_account_connect_health("acct-1", "ok")
            self.assertEqual(service.get_account_connect_health("acct-1"), "ok")
        self.assertEqual(seen, ["acct-1", "acct-1"])


if __name__ == "__main__":
    unittest.main()
