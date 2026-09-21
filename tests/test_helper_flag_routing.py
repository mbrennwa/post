# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Camel helper routing for flag mutations (#445)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


class HelperFlagRoutingTests(unittest.TestCase):
    def test_set_messages_seen_uses_local_first(self) -> None:
        service = MailService(registry=mock.Mock())
        with (
            mock.patch.object(
                service,
                "_local_first_set_flags",
                return_value={"updates": [], "queued": True},
            ) as local_first,
            mock.patch("post.mail.eds.run_on_mail_thread") as run_mail,
        ):
            result = service.set_messages_seen(
                "acct", "INBOX", ["uid-1"], seen=False
            )
        self.assertEqual(result, {"updates": [], "queued": True})
        local_first.assert_called_once_with(
            "acct",
            "INBOX",
            ["uid-1"],
            op_type="set_seen",
            seen=False,
        )
        run_mail.assert_not_called()

    def test_toggle_message_seen_uses_local_first(self) -> None:
        service = MailService(registry=mock.Mock())
        with mock.patch.object(
            service,
            "_local_first_toggle_flag",
            return_value={"flags": {"seen": False}, "queued": True},
        ) as local_first:
            result = service.toggle_message_seen("acct", "INBOX", "uid-1")
        self.assertEqual(result["queued"], True)
        local_first.assert_called_once_with(
            "acct", "INBOX", "uid-1", flag_name="seen"
        )

    def test_set_messages_flagged_mirrors_via_local_first(self) -> None:
        """Local-first flag writes still update shared folder-index disk cache."""
        service = MailService(registry=mock.Mock())
        helper_result = {
            "updates": [{"uid": "uid-1", "flags": {"flagged": True}}],
            "queued": True,
        }
        with mock.patch.object(
            service, "_local_first_set_flags", return_value=helper_result
        ) as local_first:
            result = service.set_messages_flagged(
                "acct", "INBOX", ["uid-1"], flagged=True
            )
        self.assertEqual(result, helper_result)
        local_first.assert_called_once_with(
            "acct",
            "INBOX",
            ["uid-1"],
            op_type="set_flagged",
            flagged=True,
        )


if __name__ == "__main__":
    unittest.main()
