# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import threading
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Camel", "1.2")

from gi.repository import Camel, Gio, GLib

from post.mail.eds import (
    MailService,
    _SEND_TIMEOUT_SECONDS,
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

        service._append_to_sent_folder_unlocked("acct-1", message)

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
            service._append_to_sent_folder_unlocked("acct-1", message)

        self.assertTrue(
            any("timed out" in message for message in logs.output),
            logs.output,
        )
        service._invalidate_folder_index.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
