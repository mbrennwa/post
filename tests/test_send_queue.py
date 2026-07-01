# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from post.mail.compose import ComposeAttachment
from post.mail.send_queue import (
    QueuedOutboundMessage,
    count_queued_for_account,
    enqueue_outbound_message,
    is_outbound_ready_to_send,
    is_queueable_network_error,
    list_pending_delayed_outbound_messages,
    list_queued_for_account,
    list_queued_messages_page,
    list_queued_outbound_messages,
    load_queued_attachments,
    load_queued_outbound_message,
    persist_outbound_send,
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

    def test_offline_cache_status_text(self) -> None:
        from post.mail.send_queue import offline_cache_status_text

        self.assertEqual(
            offline_cache_status_text(account_label="Work", folder_name="Inbox"),
            "Caching mail for offline use · Work · Inbox",
        )
        self.assertEqual(
            offline_cache_status_text(account_label="Work", folder_name=""),
            "Caching mail for offline use · Work · folders",
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

    def test_enqueue_with_attachments_writes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = enqueue_outbound_message(
                    QueuedOutboundMessage(
                        account_uid="account-1",
                        to=["user@example.com"],
                        cc=None,
                        bcc=None,
                        subject="With files",
                        body="Body text",
                    ),
                    attachment_payloads=[
                        ComposeAttachment(
                            filename="doc.pdf",
                            mime_type="application/pdf",
                            data=b"%PDF-fake",
                        )
                    ],
                )
                queued = list_queued_outbound_messages()
                self.assertEqual(len(queued), 1)
                message = queued[0][1]
                self.assertIsNotNone(message.attachments)
                assert message.attachments is not None
                self.assertEqual(len(message.attachments), 1)
                self.assertEqual(message.attachments[0]["filename"], "doc.pdf")

                attachment_dir = os.path.join(tmp, queue_id)
                self.assertTrue(os.path.isdir(attachment_dir))
                self.assertTrue(os.path.isfile(os.path.join(attachment_dir, "0")))

                loaded = load_queued_attachments(queue_id, message)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].filename, "doc.pdf")
                self.assertEqual(loaded[0].data, b"%PDF-fake")

                remove_queued_outbound_message(queue_id)
                self.assertEqual(list_queued_outbound_messages(), [])
                self.assertFalse(os.path.isdir(attachment_dir))

    def test_from_dict_without_attachments(self) -> None:
        message = QueuedOutboundMessage.from_dict(
            {
                "account_uid": "a",
                "to": ["b@example.com"],
                "cc": None,
                "bcc": None,
                "subject": "Hi",
                "body": "Body",
            }
        )
        self.assertIsNone(message.attachments)


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

    def test_persist_outbound_send_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = persist_outbound_send(
                    account_uid="account-1",
                    to=["user@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Hello",
                    body="Body text",
                    in_reply_to="<msg@example.com>",
                )
                loaded = load_queued_outbound_message(queue_id)
                self.assertEqual(loaded.subject, "Hello")
                self.assertEqual(loaded.in_reply_to, "<msg@example.com>")

    def test_send_after_persisted_and_ready_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                send_after = time.time() + 120
                queue_id = persist_outbound_send(
                    account_uid="account-1",
                    to=["user@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Delayed",
                    body="Body",
                    send_after=send_after,
                )
                loaded = load_queued_outbound_message(queue_id)
                self.assertEqual(loaded.send_after, send_after)
                self.assertFalse(is_outbound_ready_to_send(loaded))
                ready = QueuedOutboundMessage(
                    account_uid="account-1",
                    to=["user@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Delayed",
                    body="Body",
                    send_after=time.time() - 1,
                )
                self.assertTrue(is_outbound_ready_to_send(ready))

    def test_list_pending_delayed_outbound_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                persist_outbound_send(
                    account_uid="account-1",
                    to=["offline@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Offline",
                    body="Body",
                )
                delayed_id = persist_outbound_send(
                    account_uid="account-1",
                    to=["delayed@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Delayed",
                    body="Body",
                    send_after=time.time() + 120,
                )
                pending = list_pending_delayed_outbound_messages()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0][0], delayed_id)
                self.assertEqual(pending[0][1].subject, "Delayed")
