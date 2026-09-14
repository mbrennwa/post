# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""MailService named-job dispatcher (#425)."""

from __future__ import annotations

import unittest
from unittest import mock

from post.mail.eds import MailService


def _run_named(_name, func, /, *args, **kwargs):
    func(*args, **kwargs)
    return None


def _run_idle(callback, *args):
    callback(*args)
    return 0


class MailJobApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())

    def test_run_job_async_front_queue_delivers_value_on_idle(self) -> None:
        on_done = mock.Mock()
        with (
            mock.patch.object(
                self.service, "submit_front", side_effect=_run_named
            ) as submit_front,
            mock.patch("post.mail.eds.GLib.idle_add", side_effect=_run_idle),
        ):
            self.service.run_job_async(
                "probe", lambda: 42, queue="front", on_done=on_done
            )
        submit_front.assert_called_once()
        self.assertEqual(submit_front.call_args[0][0], "probe")
        on_done.assert_called_once_with(42, None)

    def test_run_job_async_reports_error(self) -> None:
        on_done = mock.Mock()
        error = ValueError("boom")

        def boom() -> None:
            raise error

        with (
            mock.patch.object(self.service, "submit_interactive", side_effect=_run_named),
            mock.patch("post.mail.eds.GLib.idle_add", side_effect=_run_idle),
        ):
            self.service.run_job_async("probe", boom, on_done=on_done)
        on_done.assert_called_once()
        self.assertIsNone(on_done.call_args[0][0])
        self.assertIs(on_done.call_args[0][1], error)

    def test_read_message_async_uses_front_queue(self) -> None:
        on_done = mock.Mock()
        self.service.read_message = mock.Mock(return_value={"uid": "1"})
        with (
            mock.patch.object(
                self.service, "submit_front", side_effect=_run_named
            ) as submit_front,
            mock.patch("post.mail.eds.GLib.idle_add", side_effect=_run_idle),
        ):
            self.service.read_message_async(
                "acct-1", "INBOX", "1", on_done=on_done
            )
        submit_front.assert_called_once()
        self.assertEqual(submit_front.call_args[0][0], "read_message")
        on_done.assert_called_once_with({"uid": "1"}, None)
        self.service.read_message.assert_called_once_with(
            "acct-1", "INBOX", "1", mark_seen=True
        )
