# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Helper IPC must preserve MessageNotAvailableError.reason (#422 / GOA)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.camel_runtime import CamelHelperError
from post.mail.eds import MailService, MessageNotAvailableError, MessageUnavailableReason


class HelperMessageUnavailableTests(unittest.TestCase):
    def test_helper_sign_in_miss_keeps_reason_not_vanished(self) -> None:
        """UI must not treat GOA cache misses as VANISHED (row deletion)."""
        service = MailService(registry=mock.Mock())
        details = {
            "message_uid": "uid-1",
            "folder_name": "Inbox",
            "reason": MessageUnavailableReason.NOT_CACHED_SIGN_IN,
        }
        with mock.patch.object(
            service._camel_pool,
            "call",
            side_effect=CamelHelperError(
                "MessageNotAvailableError: uid-1",
                error_type="MessageNotAvailableError",
                details=details,
            ),
        ):
            with self.assertRaises(MessageNotAvailableError) as ctx:
                service._camel_helper_call(
                    "read_message",
                    "acct",
                    ["acct", "Inbox", "uid-1"],
                    {"mark_seen": False},
                )
        self.assertEqual(
            ctx.exception.reason, MessageUnavailableReason.NOT_CACHED_SIGN_IN
        )
        self.assertEqual(ctx.exception.message_uid, "uid-1")
        self.assertEqual(ctx.exception.folder_name, "Inbox")
        self.assertNotEqual(
            ctx.exception.user_message(), "This message is no longer available."
        )

    def test_legacy_helper_string_still_maps_to_vanished(self) -> None:
        service = MailService(registry=mock.Mock())
        with mock.patch.object(
            service._camel_pool,
            "call",
            side_effect=CamelHelperError(
                "MessageNotAvailableError: uid-1",
                error_type="MessageNotAvailableError",
                details={},
            ),
        ):
            with self.assertRaises(MessageNotAvailableError) as ctx:
                service._camel_helper_call(
                    "read_message",
                    "acct",
                    ["acct", "Inbox", "uid-1"],
                )
        self.assertEqual(ctx.exception.reason, MessageUnavailableReason.VANISHED)


if __name__ == "__main__":
    unittest.main()
