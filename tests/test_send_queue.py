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
from post.mail.network_errors import (
    is_network_unavailable_error,
    is_queueable_network_error,
)
from post.mail.offline_status import offline_cache_status_text
from post.mail.send_queue import (
    QueuedOutboundMessage,
    clear_outbound_send_delay,
    count_queued_for_account,
    enqueue_outbound_message,
    format_status_send_now_tooltip,
    format_stop_sending_error_toast,
    format_stop_sending_toast,
    has_pending_send_delay,
    is_outbound_ready_to_send,
    list_pending_delayed_outbound_messages,
    list_queued_for_account,
    list_queued_messages_page,
    list_queued_outbound_messages,
    load_queued_attachments,
    load_queued_outbound_message,
    persist_outbound_send,
    queued_to_list_dict,
    read_queued_message,
    remaining_send_delay_seconds,
    remove_queued_outbound_message,
    soonest_pending_send_after,
    try_load_queued_outbound_message,
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
        exc = GLib.Error.new_literal(
            GLib.quark_from_string("camel-service-error-quark"),
            'You must be working online to complete this operation '
            '(Error resolving "lx17.hoststar.hosting": Temporary failure in name resolution)',
            2,
        )
        self.assertTrue(is_network_unavailable_error(exc))

    def test_offline_cache_status_text(self) -> None:
        self.assertEqual(
            offline_cache_status_text(account_label="Work", folder_name="Inbox"),
            "Caching mail for offline use · Work · Inbox (bodies for local headers)",
        )
        self.assertEqual(
            offline_cache_status_text(account_label="Work", folder_name=""),
            "Caching mail for offline use · Work · folders (bodies for local headers)",
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

    def test_try_load_queued_outbound_message_missing_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = persist_outbound_send(
                    account_uid="account-1",
                    to=["user@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Hello",
                    body="Body text",
                )
                loaded = try_load_queued_outbound_message(queue_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.subject, "Hello")
                remove_queued_outbound_message(queue_id)
                self.assertIsNone(try_load_queued_outbound_message(queue_id))
                with self.assertRaises(FileNotFoundError):
                    load_queued_outbound_message(queue_id)

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

    def test_list_pending_delayed_spans_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                first = persist_outbound_send(
                    account_uid="account-work",
                    to=["a@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Work delayed",
                    body="Body",
                    send_after=time.time() + 120,
                )
                second = persist_outbound_send(
                    account_uid="account-personal",
                    to=["b@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Personal delayed",
                    body="Body",
                    send_after=time.time() + 180,
                )
                pending = list_pending_delayed_outbound_messages()
                self.assertEqual(len(pending), 2)
                by_id = {queue_id: message for queue_id, message in pending}
                self.assertEqual(by_id[first].account_uid, "account-work")
                self.assertEqual(by_id[second].account_uid, "account-personal")
                self.assertTrue(has_pending_send_delay(by_id[first]))
                self.assertTrue(has_pending_send_delay(by_id[second]))

    def test_has_pending_send_delay(self) -> None:
        future = time.time() + 120
        delayed = QueuedOutboundMessage(
            account_uid="account-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Delayed",
            body="Body",
            send_after=future,
        )
        ready = QueuedOutboundMessage(
            account_uid="account-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Ready",
            body="Body",
            send_after=time.time() - 1,
        )
        immediate = QueuedOutboundMessage(
            account_uid="account-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Immediate",
            body="Body",
        )
        self.assertTrue(has_pending_send_delay(delayed))
        self.assertFalse(has_pending_send_delay(ready))
        self.assertFalse(has_pending_send_delay(immediate))

    def test_soonest_pending_send_after(self) -> None:
        now = 1_000.0
        earlier = QueuedOutboundMessage(
            account_uid="account-1",
            to=["a@example.com"],
            cc=None,
            bcc=None,
            subject="Soon",
            body="Body",
            send_after=now + 10,
        )
        later = QueuedOutboundMessage(
            account_uid="account-1",
            to=["b@example.com"],
            cc=None,
            bcc=None,
            subject="Later",
            body="Body",
            send_after=now + 30,
        )
        expired = QueuedOutboundMessage(
            account_uid="account-1",
            to=["c@example.com"],
            cc=None,
            bcc=None,
            subject="Expired",
            body="Body",
            send_after=now - 1,
        )
        immediate = QueuedOutboundMessage(
            account_uid="account-1",
            to=["d@example.com"],
            cc=None,
            bcc=None,
            subject="Immediate",
            body="Body",
        )
        self.assertEqual(
            soonest_pending_send_after(
                [later, earlier, expired, immediate], now=now
            ),
            now + 10,
        )
        self.assertIsNone(
            soonest_pending_send_after([expired, immediate], now=now)
        )

    def test_remaining_send_delay_seconds(self) -> None:
        now = 1_000.0
        self.assertEqual(remaining_send_delay_seconds(now + 30, now=now), 30)
        self.assertEqual(remaining_send_delay_seconds(now + 30.1, now=now), 31)
        self.assertEqual(remaining_send_delay_seconds(now + 0.01, now=now), 1)
        self.assertEqual(remaining_send_delay_seconds(now, now=now), 0)
        self.assertEqual(remaining_send_delay_seconds(now - 1, now=now), 0)

    def test_clear_outbound_send_delay(self) -> None:
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
                self.assertTrue(clear_outbound_send_delay(queue_id))
                loaded = load_queued_outbound_message(queue_id)
                self.assertIsNone(loaded.send_after)
                self.assertTrue(is_outbound_ready_to_send(loaded))
                self.assertEqual(list_pending_delayed_outbound_messages(), [])
                self.assertFalse(clear_outbound_send_delay(queue_id))

    def test_queued_to_list_dict_includes_send_after(self) -> None:
        send_after = time.time() + 60
        message = QueuedOutboundMessage(
            account_uid="account-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Delayed",
            body="Body",
            queued_at=1000.0,
            send_after=send_after,
        )
        item = queued_to_list_dict("queue-1", message, from_label="me@example.com")
        self.assertEqual(item["send_after"], send_after)
        immediate = queued_to_list_dict(
            "queue-2",
            QueuedOutboundMessage(
                account_uid="account-1",
                to=["user@example.com"],
                cc=None,
                bcc=None,
                subject="Immediate",
                body="Body",
                queued_at=1000.0,
            ),
            from_label="me@example.com",
        )
        self.assertNotIn("send_after", immediate)


class StatusSendNowTooltipTests(unittest.TestCase):
    def test_single_and_multiple(self) -> None:
        self.assertEqual(
            format_status_send_now_tooltip(1),
            "Send now (skip delay)",
        )
        self.assertEqual(
            format_status_send_now_tooltip(3),
            "Send 3 delayed messages now",
        )
        self.assertEqual(
            format_status_send_now_tooltip(0),
            "Send now (skip delay)",
        )


class StopSendingToastTests(unittest.TestCase):
    def test_single_message(self) -> None:
        self.assertEqual(
            format_stop_sending_toast({"mbrennwa@gmail.com": 1}),
            "Moved message to Drafts: mbrennwa@gmail.com",
        )

    def test_same_account_multiple(self) -> None:
        self.assertEqual(
            format_stop_sending_toast([("mbrennwa@gmail.com", 3)]),
            "Moved 3 messages to Drafts: mbrennwa@gmail.com",
        )

    def test_multiple_accounts_named(self) -> None:
        self.assertEqual(
            format_stop_sending_toast(
                {
                    "mbrennwa@gmail.com": 3,
                    "info@example.com": 2,
                }
            ),
            "Moved messages to Drafts: info@example.com (2), "
            "mbrennwa@gmail.com (3)",
        )

    def test_error_single_and_multi_account(self) -> None:
        self.assertEqual(
            format_stop_sending_error_toast({"mbrennwa@gmail.com": 1}),
            "Could not move message to Drafts: mbrennwa@gmail.com",
        )
        self.assertEqual(
            format_stop_sending_error_toast({"mbrennwa@gmail.com": 2}),
            "Could not move messages to Drafts: mbrennwa@gmail.com (2)",
        )
        self.assertEqual(
            format_stop_sending_error_toast(
                {
                    "mbrennwa@gmail.com": 3,
                    "info@example.com": 2,
                }
            ),
            "Could not move messages to Drafts: info@example.com (2), "
            "mbrennwa@gmail.com (3)",
        )
