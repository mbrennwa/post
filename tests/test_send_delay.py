# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from post.mail.send_delay import OutboundSendDelayScheduler
from post.mail.send_errors import SendError
from post.mail.send_queue import (
    QueuedOutboundMessage,
    enqueue_outbound_message,
    is_outbound_parked,
    load_queued_outbound_message,
    park_outbound_message,
)


def _run_idle_add(func, *args):
    func(*args)
    return 0


class DelayedSendErrorTests(unittest.TestCase):
    def test_permanent_error_parks_and_toasts(self) -> None:
        mail = mock.Mock()
        mail.deliver_outbound_queue_item.side_effect = SendError(
            "This message is too large to send."
        )
        errors: list[str] = []
        scheduler = OutboundSendDelayScheduler(
            mail,
            on_send_error=errors.append,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp),
                mock.patch("post.mail.send_delay.GLib.idle_add", side_effect=_run_idle_add),
            ):
                queue_id = enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="acct-1",
                        to=["user@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Hello",
                        body="Body",
                    )
                )
                scheduler._deliver_worker(queue_id)
                parked = load_queued_outbound_message(queue_id)

        self.assertTrue(is_outbound_parked(parked))
        self.assertEqual(len(errors), 1)
        self.assertIn("too large to send", errors[0])
        self.assertIn("still in Outbox", errors[0])
        self.assertIn("Hello", errors[0])

    def test_send_now_retries_parked_item(self) -> None:
        mail = mock.Mock()
        scheduler = OutboundSendDelayScheduler(mail)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="acct-1",
                        to=["user@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Hello",
                        body="Body",
                    )
                )
                park_outbound_message(queue_id, "This message is too large to send.")
                scheduler._send_now_worker(queue_id)
                loaded = load_queued_outbound_message(queue_id)

        mail.deliver_outbound_queue_item.assert_called_once_with(queue_id)
        self.assertFalse(is_outbound_parked(loaded))
