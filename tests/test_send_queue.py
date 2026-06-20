# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from post.mail.send_queue import (
    QueuedOutboundMessage,
    count_queued_for_account,
    enqueue_outbound_message,
    is_queueable_network_error,
    list_queued_for_account,
    list_queued_messages_page,
    list_queued_outbound_messages,
    queued_to_list_dict,
    read_queued_message,
    remove_queued_outbound_message,
)


class QueueableNetworkErrorTests(unittest.TestCase):
    def test_timeout_is_queueable(self) -> None:
        self.assertTrue(is_queueable_network_error(TimeoutError()))

    def test_remote_connection_failure_is_queueable(self) -> None:
        exc = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"),
            "Could not connect to mail.example.com: Network is unreachable",
            39,
        )
        self.assertTrue(is_queueable_network_error(exc))

    def test_localhost_refused_is_not_queueable(self) -> None:
        exc = GLib.Error.new_literal(
            GLib.quark_from_string("g-io-error-quark"),
            "Could not connect to 127.0.0.1: Connection refused",
            39,
        )
        self.assertFalse(is_queueable_network_error(exc))

    def test_camel_offline_service_error(self) -> None:
        from post.mail.send_queue import is_network_unavailable_error

        exc = GLib.Error.new_literal(
            GLib.quark_from_string("camel-service-error-quark"),
            'You must be working online to complete this operation '
            '(Error resolving "lx17.hoststar.hosting": Temporary failure in name resolution)',
            2,
        )
        self.assertTrue(is_network_unavailable_error(exc))

    def test_offline_status_with_queue(self) -> None:
        from post.mail.send_queue import offline_status_text

        self.assertEqual(offline_status_text(queued_count=0), "Offline")
        self.assertEqual(
            offline_status_text(queued_count=2),
            "Offline · 2 messages queued",
        )


class OutboundQueueStorageTests(unittest.TestCase):
    def test_enqueue_list_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="account-1",
                        to=["user@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Hello",
                        body="Body text",
                    )
                )
                queued = list_queued_outbound_messages()
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0][0], queue_id)
                self.assertEqual(queued[0][1].subject, "Hello")

                remove_queued_outbound_message(queue_id)
                self.assertEqual(list_queued_outbound_messages(), [])

    def test_enqueue_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="account-1",
                        to=["user@example.com"],
                        cc=["cc@example.com"],
                        bcc=None,
                        subject="Hello",
                        body="Body text",
                        in_reply_to="<abc@example.com>",
                    )
                )
                files = [name for name in os.listdir(tmp) if name.endswith(".json")]
                self.assertEqual(len(files), 1)
                with open(os.path.join(tmp, files[0]), encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertEqual(data["to"], ["user@example.com"])
                self.assertEqual(data["in_reply_to"], "<abc@example.com>")


class OutboxAccountFilterTests(unittest.TestCase):
    def test_count_and_list_by_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="a1",
                        to=["one@example.com"],
                        cc=None,
                        bcc=None,
                        subject="One",
                        body="",
                        queued_at=100.0,
                    )
                )
                enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="a2",
                        to=["two@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Two",
                        body="",
                        queued_at=200.0,
                    )
                )
                self.assertEqual(count_queued_for_account("a1"), 1)
                self.assertEqual(count_queued_for_account("a2"), 1)
                self.assertEqual(count_queued_for_account("missing"), 0)

                items = list_queued_for_account("a2")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0][1].subject, "Two")

    def test_list_page_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="acct",
                        to=["dest@example.com"],
                        cc=["cc@example.com"],
                        bcc=None,
                        subject="Queued",
                        body="Hello queued",
                        queued_at=1_700_000_000.0,
                    )
                )
                listed = queued_to_list_dict(
                    queue_id,
                    list_queued_for_account("acct")[0][1],
                    from_label="me@example.com",
                )
                self.assertEqual(listed["preview_to"], "dest@example.com")
                self.assertEqual(listed["from"], "me@example.com")

                page, unread, total, has_more = list_queued_messages_page(
                    "acct",
                    from_label="me@example.com",
                    offset=0,
                    limit=10,
                )
                self.assertEqual(unread, 0)
                self.assertEqual(total, 1)
                self.assertFalse(has_more)
                self.assertEqual(page[0]["uid"], queue_id)

                msg = read_queued_message(
                    queue_id,
                    account_uid="acct",
                    from_label="me@example.com",
                )
                self.assertEqual(msg["body_plain"], "Hello queued")
                self.assertEqual(msg["to"], "dest@example.com")
                self.assertEqual(msg["cc"], "cc@example.com")
