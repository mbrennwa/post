# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest import mock

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from post.compose_window import (
    OutboundSendRequest,
    _finish_outbound_send,
    run_outbound_send,
)
from post.mail.send_errors import MESSAGE_QUEUED, SendError, SendQueued


def _run_idle_add(func, *args):
    func(*args)
    return 0


class _ImmediateMailIoThread:
    def submit(self, func, /, *args, **kwargs) -> None:
        func(*args, **kwargs)

    def run_sync(self, func, /, *args, **kwargs):
        return func(*args, **kwargs)


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **__ignored):
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()


class RunOutboundSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = OutboundSendRequest(
            account_uid="acct-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Hello",
            body="Body",
            in_reply_to=None,
            references=None,
            attachments=None,
        )
        self.status_messages: list[str] = []
        self.set_status = self.status_messages.append
        self.mail = mock.Mock()

    @mock.patch(
        "post.compose_window.get_mail_io_thread",
        return_value=_ImmediateMailIoThread(),
    )
    @mock.patch("post.compose_window.prepare_camel_worker_thread")
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.apply_send_debug_delay")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_success_sets_message_sent(
        self, _idle_add, _persist, _delay, _queue_id, _prepare, _io
    ) -> None:
        outbox_changed = mock.Mock()
        run_outbound_send(
            mail=self.mail,
            parent=None,
            set_status=self.set_status,
            on_outbox_changed=outbox_changed,
            on_draft_saved=None,
            request=self.request,
        )
        self.mail.claim_outbound_delivery.assert_called_once_with("queue-1")
        self.mail.begin_outbound_send.assert_called_once()
        self.mail.end_outbound_send.assert_called_once()
        self.mail.release_outbound_delivery.assert_called_once_with("queue-1")
        outbox_changed.assert_called()
        self.mail.deliver_outbound_queue_item.assert_called_once_with("queue-1")
        self.assertEqual(self.status_messages, ["Message sent"])

    @mock.patch(
        "post.compose_window.get_mail_io_thread",
        return_value=_ImmediateMailIoThread(),
    )
    @mock.patch("post.compose_window.prepare_camel_worker_thread")
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_send_queued_updates_status_and_outbox(
        self, _idle_add, _persist, _queue_id, _prepare, _io
    ) -> None:
        self.mail.deliver_outbound_queue_item.side_effect = SendQueued(MESSAGE_QUEUED)
        outbox_changed = mock.Mock()

        run_outbound_send(
            mail=self.mail,
            parent=None,
            set_status=self.set_status,
            on_outbox_changed=outbox_changed,
            on_draft_saved=None,
            request=self.request,
        )

        self.assertGreaterEqual(outbox_changed.call_count, 1)
        self.assertEqual(self.status_messages, [MESSAGE_QUEUED])

    @mock.patch(
        "post.compose_window.get_mail_io_thread",
        return_value=_ImmediateMailIoThread(),
    )
    @mock.patch("post.compose_window.prepare_camel_worker_thread")
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_error_shows_toast_on_parent(
        self, _idle_add, _persist, show_error_toast, _queue_id, _prepare, _io
    ) -> None:
        self.mail.deliver_outbound_queue_item.side_effect = SendError("SMTP failed")
        parent = mock.Mock()
        outbox_changed = mock.Mock()

        run_outbound_send(
            mail=self.mail,
            parent=parent,
            set_status=self.set_status,
            on_outbox_changed=outbox_changed,
            on_draft_saved=None,
            request=self.request,
        )

        show_error_toast.assert_called_once_with(
            parent,
            "SMTP failed Message saved in Outbox.",
        )
        self.assertEqual(self.status_messages, [])
        outbox_changed.assert_called()

    @mock.patch(
        "post.compose_window.get_mail_io_thread",
        return_value=_ImmediateMailIoThread(),
    )
    @mock.patch("post.compose_window.prepare_camel_worker_thread")
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_success_deletes_draft_before_status(
        self, _idle_add, _persist, _queue_id, _prepare, _io
    ) -> None:
        request = OutboundSendRequest(
            account_uid="acct-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Hello",
            body="Body",
            in_reply_to=None,
            references=None,
            attachments=None,
            draft_folder="Drafts",
            draft_uid="42",
        )
        on_draft_saved = mock.Mock()
        outbox_changed = mock.Mock()

        with mock.patch("post.compose_window.threading.Thread", ImmediateThread):
            run_outbound_send(
                mail=self.mail,
                parent=None,
                set_status=self.set_status,
                on_outbox_changed=outbox_changed,
                on_draft_saved=on_draft_saved,
                request=request,
            )

        self.mail.delete_draft.assert_called_once_with("acct-1", "Drafts", "42")
        on_draft_saved.assert_called_once()
        self.assertEqual(self.status_messages, ["Message sent"])

    @mock.patch(
        "post.compose_window.get_mail_io_thread",
        return_value=_ImmediateMailIoThread(),
    )
    @mock.patch("post.compose_window.prepare_camel_worker_thread")
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch(
        "post.compose_window.persist_outbound_send",
        side_effect=OSError("disk full"),
    )
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_persist_failure_shows_toast(
        self, _idle_add, persist, show_error_toast, _queue_id, _prepare, _io
    ) -> None:
        parent = mock.Mock()
        run_outbound_send(
            mail=self.mail,
            parent=parent,
            set_status=self.set_status,
            on_outbox_changed=None,
            on_draft_saved=None,
            request=self.request,
        )
        persist.assert_called_once()
        show_error_toast.assert_called_once()
        self.mail.deliver_outbound_queue_item.assert_not_called()


class FinishOutboundSendTests(unittest.TestCase):
    @mock.patch("post.compose_window.show_error_toast")
    def test_finish_reports_error_to_parent(self, show_error_toast) -> None:
        parent = mock.Mock()
        set_status = mock.Mock()
        outbox_changed = mock.Mock()
        request = OutboundSendRequest(
            account_uid="acct-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Hello",
            body="Body",
            in_reply_to=None,
            references=None,
            attachments=None,
            queue_id="queue-1",
        )

        _finish_outbound_send(
            parent,
            set_status,
            None,
            outbox_changed,
            mock.Mock(),
            request,
            SendError("Could not send"),
            None,
        )

        show_error_toast.assert_called_once_with(
            parent,
            "Could not send Message saved in Outbox.",
        )
        set_status.assert_not_called()
        outbox_changed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
