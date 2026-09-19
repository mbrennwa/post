# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Camel helper routing for flag mutations (#445)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class HelperFlagRoutingTests(unittest.TestCase):
    def test_set_messages_seen_uses_helper(self) -> None:
        service = MailService(registry=mock.Mock())
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch.object(
                service,
                "_camel_helper_call",
                return_value={"unread": 1, "total": 2},
            ) as helper_call,
            mock.patch("post.mail.eds.run_on_mail_thread") as run_mail,
        ):
            result = service.set_messages_seen(
                "acct", "INBOX", ["uid-1"], seen=False
            )
        self.assertEqual(result, {"unread": 1, "total": 2})
        helper_call.assert_called_once_with(
            "set_messages_seen",
            "acct",
            ["acct", "INBOX", ["uid-1"]],
            {"seen": False},
        )
        run_mail.assert_not_called()

    def test_set_messages_seen_uses_mail_thread_when_helpers_disabled_for_tests(
        self,
    ) -> None:
        """In-process path is only for ``POST_MAIL_TEST_NO_CAMEL_HELPERS``."""
        service = MailService(registry=mock.Mock())
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=False),
            mock.patch.object(service, "_camel_helper_call") as helper_call,
            mock.patch(
                "post.mail.eds.run_on_mail_thread",
                return_value={"unread": 0, "total": 1},
            ) as run_mail,
        ):
            result = service.set_messages_seen(
                "acct", "INBOX", ["uid-1"], seen=True
            )
        self.assertEqual(result, {"unread": 0, "total": 1})
        helper_call.assert_not_called()
        run_mail.assert_called_once()

    def test_toggle_message_seen_uses_helper(self) -> None:
        service = MailService(registry=mock.Mock())
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch.object(
                service, "_camel_helper_call", return_value={"seen": False}
            ) as helper_call,
        ):
            result = service.toggle_message_seen("acct", "INBOX", "uid-1")
        self.assertEqual(result, {"seen": False})
        helper_call.assert_called_once_with(
            "toggle_message_seen",
            "acct",
            ["acct", "INBOX", "uid-1"],
        )

    def test_set_messages_flagged_mirrors_disk_cache(self) -> None:
        """Helper flag writes must update shared folder-index disk cache (#445)."""
        service = MailService(registry=mock.Mock())
        helper_result = {
            "updates": [{"uid": "uid-1", "flags": {"flagged": True}}],
            "queued": False,
        }
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch.object(
                service, "_camel_helper_call", return_value=helper_result
            ),
            mock.patch.object(service, "_mirror_flag_result_to_folder_caches") as mirror,
        ):
            result = service.set_messages_flagged(
                "acct", "INBOX", ["uid-1"], flagged=True
            )
        self.assertEqual(result, helper_result)
        mirror.assert_called_once_with("acct", "INBOX", helper_result)


if __name__ == "__main__":
    unittest.main()
