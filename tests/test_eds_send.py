# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Camel", "1.2")

from gi.repository import Camel, Gio, GLib

from post.mail.camel_runtime import CamelHelperError
from post.mail.eds import (
    MailService,
    _SEND_TIMEOUT_SECONDS,
)
from post.mail.send_errors import SentCopyNotFiled
from post.mail.send_queue import (
    QueuedOutboundMessage,
    enqueue_outbound_message,
    is_outbound_parked,
    load_queued_outbound_message,
)


class AppendToSentFolderTests(unittest.TestCase):
    @mock.patch("post.mail.eds.folder_get_message_info")
    @mock.patch("post.mail.eds.threading.Timer")
    def test_append_passes_cancellable_and_uses_timeout_timer(
        self, timer_cls, folder_get_message_info
    ) -> None:
        timer = timer_cls.return_value
        folder = mock.Mock()
        folder.append_message_sync.return_value = (True, "1")
        message = mock.Mock()
        info_after = mock.Mock()
        info_after.get_flags.return_value = Camel.MessageFlags.SEEN
        folder_get_message_info.return_value = info_after

        service = MailService(registry=mock.Mock())
        service._sent_folder_name_unlocked = mock.Mock(return_value="Sent")
        service._open_folder_unlocked = mock.Mock(return_value=folder)
        service._invalidate_folder_index = mock.Mock()
        service._mark_message_seen_unlocked = mock.Mock()

        self.assertTrue(service._append_to_sent_folder_unlocked("acct-1", message))

        folder.append_message_sync.assert_called_once()
        _message, info, cancellable = folder.append_message_sync.call_args[0]
        self.assertIs(message, _message)
        self.assertIsNotNone(info)
        self.assertTrue(info.get_flags() & Camel.MessageFlags.SEEN)
        self.assertIsInstance(cancellable, Gio.Cancellable)
        timer_cls.assert_called_once_with(_SEND_TIMEOUT_SECONDS, cancellable.cancel)
        timer.start.assert_called_once()
        timer.cancel.assert_called_once()
        service._mark_message_seen_unlocked.assert_not_called()
        service._invalidate_folder_index.assert_called_once_with("acct-1", "Sent")

    @mock.patch("post.mail.eds.folder_get_message_info")
    @mock.patch("post.mail.eds.threading.Timer")
    def test_append_marks_unread_copy_seen_after_append(
        self, timer_cls, folder_get_message_info
    ) -> None:
        timer = timer_cls.return_value
        folder = mock.Mock()
        folder.append_message_sync.return_value = (True, "42")
        message = mock.Mock()
        info_after = mock.Mock()
        info_after.get_flags.return_value = 0
        folder_get_message_info.return_value = info_after

        service = MailService(registry=mock.Mock())
        service._sent_folder_name_unlocked = mock.Mock(return_value="Sent")
        service._open_folder_unlocked = mock.Mock(return_value=folder)
        service._invalidate_folder_index = mock.Mock()
        service._mark_message_seen_unlocked = mock.Mock()

        service._append_to_sent_folder_unlocked("acct-1", message)

        service._mark_message_seen_unlocked.assert_called_once_with(
            folder, "acct-1", "Sent", "42"
        )

    @mock.patch("post.mail.eds.threading.Timer")
    def test_append_timeout_is_logged_not_raised(self, timer_cls) -> None:
        timer = timer_cls.return_value
        folder = mock.Mock()

        def append_side_effect(_message, _info, cancellable) -> tuple[bool, str]:
            cancellable.cancel()
            raise GLib.Error.new_literal(
                GLib.quark_from_string("g-io-error-quark"),
                "Operation was cancelled",
                19,
            )

        folder.append_message_sync.side_effect = append_side_effect
        message = mock.Mock()

        service = MailService(registry=mock.Mock())
        service._sent_folder_name_unlocked = mock.Mock(return_value="Sent")
        service._open_folder_unlocked = mock.Mock(return_value=folder)
        service._invalidate_folder_index = mock.Mock()

        with self.assertLogs("post.mail.eds", level="WARNING") as logs:
            stored = service._append_to_sent_folder_unlocked("acct-1", message)

        self.assertFalse(stored)

        self.assertTrue(
            any("timed out" in message for message in logs.output),
            logs.output,
        )
        service._invalidate_folder_index.assert_not_called()

    def test_missing_folder_is_not_stored(self) -> None:
        service = MailService(registry=mock.Mock())
        service._sent_folder_name_unlocked = mock.Mock(return_value="Sent")
        service._open_folder_unlocked = mock.Mock(return_value=None)

        self.assertFalse(
            service._append_to_sent_folder_unlocked("acct-1", mock.Mock())
        )

    def test_no_sent_folder_configured_counts_as_stored(self) -> None:
        service = MailService(registry=mock.Mock())
        service._sent_folder_name_unlocked = mock.Mock(return_value=None)

        self.assertTrue(
            service._append_to_sent_folder_unlocked("acct-1", mock.Mock())
        )


class SentMessageInfoTests(unittest.TestCase):
    def test_sent_message_info_sets_seen_flag(self) -> None:
        service = MailService(registry=mock.Mock())
        info = service._sent_message_info()
        self.assertIsNotNone(info)
        self.assertTrue(info.get_flags() & Camel.MessageFlags.SEEN)


class FromQueueCamelSendTests(unittest.TestCase):
    @mock.patch("post.mail.eds.threading.Timer")
    @mock.patch("post.mail.eds.build_plain_mime_message")
    def test_from_queue_uses_camel_transport(
        self, build_plain_mime_message, timer_cls
    ) -> None:
        timer_cls.return_value = mock.Mock()
        mime_message = mock.Mock()
        build_plain_mime_message.return_value = mime_message

        service = MailService(registry=mock.Mock())
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.transport_uid = "smtp-uid"
        service.get_account = mock.Mock(return_value=account)

        transport = mock.Mock()
        transport.send_to_sync.return_value = (True, False)
        service._get_transport_unlocked = mock.Mock(return_value=transport)
        service._append_sent_copy_and_finish_queue_item = mock.Mock()

        service._send_message_unlocked(
            "acct-1",
            to=["to@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Body",
            in_reply_to=None,
            references=None,
            from_queue=True,
            queue_id="queue-1",
        )

        service._get_transport_unlocked.assert_called_once()
        transport.send_to_sync.assert_called_once()
        service._append_sent_copy_and_finish_queue_item.assert_called_once()


class OutboundSendTrackingTests(unittest.TestCase):
    def test_wait_for_outbound_sends_blocks_until_complete(self) -> None:
        service = MailService(registry=mock.Mock())
        service._begin_outbound_send()
        done = threading.Event()

        def release() -> None:
            service._end_outbound_send()
            done.set()

        threading.Timer(0.05, release).start()
        service.wait_for_outbound_sends(timeout=1.0)
        self.assertTrue(done.is_set())
        self.assertFalse(service.outbound_sends_pending())

    def test_wait_for_outbound_sends_times_out_and_resets_counter(self) -> None:
        service = MailService(registry=mock.Mock())
        service._begin_outbound_send()
        completed = service.wait_for_outbound_sends(timeout=0.05)
        self.assertFalse(completed)
        service._reset_outbound_send_counter_after_timeout()
        self.assertFalse(service.outbound_sends_pending())

    @mock.patch("post.mail.eds.GLib.idle_add", side_effect=lambda fn, *a: fn(*a) or 0)
    def test_when_outbound_sends_complete_runs_callback_after_send(
        self, _idle_add
    ) -> None:
        service = MailService(registry=mock.Mock())
        service._begin_outbound_send()
        callback = mock.Mock()

        def release() -> None:
            service._end_outbound_send()

        threading.Timer(0.05, release).start()
        service.when_outbound_sends_complete(callback, timeout=1.0)
        threading.Event().wait(0.2)
        callback.assert_called_once()

    @mock.patch("post.mail.eds.GLib.idle_add", side_effect=lambda fn, *a: fn(*a) or 0)
    @mock.patch.object(MailService, "shutdown_sync")
    def test_shutdown_async_runs_off_caller_then_callback(
        self, shutdown_sync, _idle_add
    ) -> None:
        service = MailService(registry=mock.Mock())
        callback = mock.Mock()
        service.shutdown_async(callback)
        threading.Event().wait(0.2)
        shutdown_sync.assert_called_once()
        callback.assert_called_once()


class FolderTransferTrackingTests(unittest.TestCase):
    def test_wait_for_folder_transfers_blocks_until_complete(self) -> None:
        service = MailService(registry=mock.Mock())
        service.begin_folder_transfer()
        done = threading.Event()

        def release() -> None:
            service.end_folder_transfer()
            done.set()

        threading.Timer(0.05, release).start()
        service.wait_for_folder_transfers(timeout=1.0)
        self.assertTrue(done.is_set())
        self.assertFalse(service.folder_transfers_pending())

    def test_wait_for_folder_transfers_times_out_and_resets_counter(self) -> None:
        service = MailService(registry=mock.Mock())
        service.begin_folder_transfer()
        completed = service.wait_for_folder_transfers(timeout=0.05)
        self.assertFalse(completed)
        service._reset_folder_transfer_counter_after_timeout()
        self.assertFalse(service.folder_transfers_pending())


class SentCopyAfterSendTests(unittest.TestCase):
    def _service(self) -> tuple[MailService, mock.Mock]:
        service = MailService(registry=mock.Mock())
        account = mock.Mock()
        account.from_address = "user@example.com"
        account.from_name = "User"
        account.email = "user@example.com"
        account.transport_uid = "smtp-uid"
        service.get_account = mock.Mock(return_value=account)
        transport = mock.Mock()
        transport.send_to_sync.return_value = (True, False)
        service._get_transport_unlocked = mock.Mock(return_value=transport)
        return service, transport

    def _enqueue(self) -> str:
        return enqueue_outbound_message(
            QueuedOutboundMessage(
                account_uid="acct-1",
                to=["to@example.com"],
                cc=None,
                bcc=None,
                subject="Hi",
                body="Body",
            )
        )

    @mock.patch("post.mail.eds.threading.Timer")
    @mock.patch("post.mail.eds.build_plain_mime_message")
    def test_failed_append_keeps_queue_and_retry_skips_smtp(
        self, build_plain_mime_message, timer_cls
    ) -> None:
        timer_cls.return_value = mock.Mock()
        build_plain_mime_message.return_value = mock.Mock()
        service, transport = self._service()
        service._append_to_sent_folder_unlocked = mock.Mock(return_value=False)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = self._enqueue()
                with self.assertRaises(SentCopyNotFiled):
                    service._send_message_unlocked(
                        "acct-1",
                        to=["to@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Hi",
                        body="Body",
                        in_reply_to=None,
                        references=None,
                        from_queue=True,
                        queue_id=queue_id,
                    )
                kept = load_queued_outbound_message(queue_id)
                self.assertTrue(os.path.exists(os.path.join(tmp, f"{queue_id}.json")))
                self.assertTrue(kept.submitted)
                self.assertTrue(kept.message_id)
                self.assertFalse(is_outbound_parked(kept))
                self.assertIsNotNone(kept.sent_copy_error)

                with self.assertRaises(SentCopyNotFiled):
                    service._send_message_unlocked(
                        "acct-1",
                        to=["to@example.com"],
                        cc=None,
                        bcc=None,
                        subject="Hi",
                        body="Body",
                        in_reply_to=None,
                        references=None,
                        from_queue=True,
                        queue_id=queue_id,
                    )

        transport.send_to_sync.assert_called_once()

    @mock.patch("post.mail.eds.threading.Timer")
    @mock.patch("post.mail.eds.build_plain_mime_message")
    def test_successful_append_removes_queue_file(
        self, build_plain_mime_message, timer_cls
    ) -> None:
        timer_cls.return_value = mock.Mock()
        build_plain_mime_message.return_value = mock.Mock()
        service, transport = self._service()
        service._append_to_sent_folder_unlocked = mock.Mock(return_value=True)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("post.mail.send_queue.outbox_dir", return_value=tmp):
                queue_id = self._enqueue()
                service._send_message_unlocked(
                    "acct-1",
                    to=["to@example.com"],
                    cc=None,
                    bcc=None,
                    subject="Hi",
                    body="Body",
                    in_reply_to=None,
                    references=None,
                    from_queue=True,
                    queue_id=queue_id,
                )
                self.assertFalse(os.path.exists(os.path.join(tmp, f"{queue_id}.json")))

        transport.send_to_sync.assert_called_once()

    def test_deliver_runs_in_the_account_helper(self) -> None:
        service = MailService(registry=mock.Mock())
        queued = QueuedOutboundMessage(
            account_uid="acct-1",
            to=["to@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Body",
        )
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch(
                "post.mail.eds.load_queued_outbound_message", return_value=queued
            ),
            mock.patch.object(service, "_camel_helper_call") as helper_call,
            mock.patch.object(service, "_deliver_outbound_queue_item_impl") as impl,
        ):
            service.deliver_outbound_queue_item("queue-1")

        helper_call.assert_called_once()
        self.assertEqual(helper_call.call_args.args[0], "deliver_outbound_queue_item")
        self.assertEqual(helper_call.call_args.args[1], "acct-1")
        impl.assert_not_called()

    def test_helper_filing_failure_is_not_a_generic_send_error(self) -> None:
        service = MailService(registry=mock.Mock())
        queued = QueuedOutboundMessage(
            account_uid="acct-1",
            to=["to@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Body",
        )
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch(
                "post.mail.eds.load_queued_outbound_message", return_value=queued
            ),
            mock.patch.object(
                service,
                "_camel_helper_call",
                side_effect=CamelHelperError(
                    "not filed", error_type="SentCopyNotFiled"
                ),
            ),
        ):
            with self.assertRaises(SentCopyNotFiled):
                service.deliver_outbound_queue_item("queue-1")

    def test_flush_send_queue_runs_in_the_account_helper(self) -> None:
        service = MailService(registry=mock.Mock())
        queued = QueuedOutboundMessage(
            account_uid="acct-1",
            to=["to@example.com"],
            cc=None,
            bcc=None,
            subject="Hi",
            body="Body",
        )
        payload = {
            "sent": 1,
            "error_message": None,
            "failed_account_uid": None,
            "parked_count": 0,
            "sign_in_required": False,
        }
        with (
            mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True),
            mock.patch(
                "post.mail.eds.list_queued_outbound_messages",
                return_value=[("queue-1", queued)],
            ),
            mock.patch.object(
                service, "account_may_contact_server", return_value=True
            ),
            mock.patch.object(
                service, "_camel_helper_call", return_value=payload
            ) as helper_call,
            mock.patch.object(service, "_flush_send_queue_unlocked") as unlocked,
        ):
            result = service.flush_send_queue()

        helper_call.assert_called_once()
        self.assertEqual(helper_call.call_args.args[0], "flush_account_send_queue")
        self.assertEqual(helper_call.call_args.args[1], "acct-1")
        unlocked.assert_not_called()
        self.assertEqual(result.sent, 1)

    def test_empty_sent_uri_uses_type_sent(self) -> None:
        service = MailService(registry=mock.Mock())
        account = mock.Mock()
        account.identity_uid = "ident-1"
        service.get_account = mock.Mock(return_value=account)
        submission = mock.Mock()
        submission.get_use_sent_folder.return_value = True
        submission.get_sent_folder.return_value = ""
        identity = mock.Mock()
        identity.has_extension.return_value = True
        identity.get_extension.return_value = submission
        service.registry.ref_source.return_value = identity
        service._folder_tree_cache["acct-1"] = [
            {
                "full_name": "Outbox",
                "display_name": "Outbox",
                "flags": int(Camel.FolderInfoFlags.TYPE_OUTBOX),
            },
            {
                "full_name": "Sent",
                "display_name": "Sent",
                "flags": int(Camel.FolderInfoFlags.TYPE_SENT),
            },
        ]

        self.assertEqual(service._sent_folder_name_unlocked("acct-1"), "Sent")


if __name__ == "__main__":
    unittest.main()
