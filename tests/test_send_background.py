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
    SavedDraftNotification,
    _finish_outbound_send,
    run_outbound_send,
)
from post.mail.send_errors import MESSAGE_QUEUED, SendError, SendQueued


def _run_idle_add(func, *args):
    func(*args)
    return 0


class RunOutboundSendTests(unittest.TestCase):
    def setUp(self) -> None:
        delay_patcher = mock.patch(
            "post.compose_window.get_send_delay_seconds",
            return_value=0,
        )
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)
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
        self.mail.get_account.return_value = mock.Mock(from_name="Alice")

        def _run_named(_name, func, /, *args, **kwargs):
            func(*args, **kwargs)

        self.mail.submit_interactive.side_effect = _run_named

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_success_sets_message_sent(
        self, _idle_add, _persist, _queue_id
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

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_send_queued_updates_status_and_outbox(
        self, _idle_add, _persist, _queue_id
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

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_error_shows_toast_on_parent(
        self, _idle_add, _persist, show_error_toast, _queue_id
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
            "“Hello” could not be sent. The message is still in Outbox. SMTP failed",
        )
        self.assertEqual(self.status_messages, [])
        outbox_changed.assert_called()

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_compose_validation_skips_outbox(
        self, _idle_add, persist, show_error_toast, _queue_id
    ) -> None:
        parent = mock.Mock()
        request = OutboundSendRequest(
            account_uid="acct-1",
            to=["user@example.com"],
            cc=None,
            bcc=None,
            subject="Hello\r\nBcc: evil@example.com",
            body="Body",
            in_reply_to=None,
            references=None,
            attachments=None,
        )

        run_outbound_send(
            mail=self.mail,
            parent=parent,
            set_status=self.set_status,
            on_outbox_changed=None,
            on_draft_saved=None,
            request=request,
        )

        persist.assert_not_called()
        self.mail.deliver_outbound_queue_item.assert_not_called()
        show_error_toast.assert_called_once_with(
            parent,
            "Subject must not contain line breaks.",
        )

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_success_deletes_draft_before_status(
        self, _idle_add, _persist, _queue_id
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
        notification = on_draft_saved.call_args.args[0]
        self.assertIsInstance(notification, SavedDraftNotification)
        self.assertTrue(notification.removed)
        self.assertEqual(notification.previous_uid, "42")
        self.assertEqual(self.status_messages, ["Message sent"])

    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch(
        "post.compose_window.persist_outbound_send",
        side_effect=OSError("disk full"),
    )
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_persist_failure_shows_toast(
        self, _idle_add, persist, show_error_toast, _queue_id
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

    @mock.patch("post.compose_window.set_outbound_send_after")
    @mock.patch("post.compose_window.get_send_delay_seconds", return_value=30)
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_delayed_send_clears_status_hint(
        self, _idle_add, persist, _queue_id, _delay, set_send_after
    ) -> None:
        outbox_changed = mock.Mock()
        on_delayed_send = mock.Mock()

        run_outbound_send(
            mail=self.mail,
            parent=None,
            set_status=self.set_status,
            on_outbox_changed=outbox_changed,
            on_draft_saved=None,
            on_delayed_send=on_delayed_send,
            request=self.request,
        )

        self.mail.deliver_outbound_queue_item.assert_not_called()
        persist.assert_called_once()
        self.assertIsNone(persist.call_args.kwargs.get("send_after"))
        set_send_after.assert_called_once()
        self.assertEqual(set_send_after.call_args.args[0], "queue-1")
        on_delayed_send.assert_called_once()
        self.assertEqual(on_delayed_send.call_args.args[0], "queue-1")
        outbox_changed.assert_called()
        # Empty hint so live countdown owns the status bar (#429).
        self.assertEqual(self.status_messages, [""])

    @mock.patch("post.compose_window.set_outbound_send_after")
    @mock.patch("post.compose_window.show_error_toast")
    @mock.patch("post.compose_window.get_send_delay_seconds", return_value=30)
    @mock.patch("post.compose_window.new_outbound_queue_id", return_value="queue-1")
    @mock.patch("post.compose_window.persist_outbound_send", return_value="queue-1")
    @mock.patch("post.compose_window.GLib.idle_add", side_effect=_run_idle_add)
    def test_delayed_send_preflight_failure_toasts_without_scheduling(
        self,
        _idle_add,
        persist,
        _queue_id,
        _delay,
        show_error_toast,
        set_send_after,
    ) -> None:
        """Dead GOA must surface immediately — before the send delay (#488)."""
        from post.mail.network_errors import TOKEN_EXPIRED_FOLDER_MESSAGE

        self.mail.ensure_account_ready_to_send.side_effect = SendError(
            TOKEN_EXPIRED_FOLDER_MESSAGE
        )
        parent = mock.Mock()
        on_delayed_send = mock.Mock()
        outbox_changed = mock.Mock()

        run_outbound_send(
            mail=self.mail,
            parent=parent,
            set_status=self.set_status,
            on_outbox_changed=outbox_changed,
            on_draft_saved=None,
            on_delayed_send=on_delayed_send,
            request=self.request,
        )

        persist.assert_called_once()
        self.assertIsNone(persist.call_args.kwargs.get("send_after"))
        set_send_after.assert_not_called()
        on_delayed_send.assert_not_called()
        self.mail.deliver_outbound_queue_item.assert_not_called()
        show_error_toast.assert_called_once()
        toast = show_error_toast.call_args.args[1]
        self.assertIn("Sign-in expired", toast)
        self.assertIn("Hello", toast)
        self.assertIn("still in Outbox", toast)
        outbox_changed.assert_called()


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
            "“Hello” could not be sent. The message is still in Outbox. Could not send",
        )
        set_status.assert_not_called()
        outbox_changed.assert_called_once()

    @mock.patch("post.compose_window.show_error_toast")
    def test_finish_sign_in_error_names_message(self, show_error_toast) -> None:
        from post.mail.network_errors import TOKEN_EXPIRED_FOLDER_MESSAGE

        parent = mock.Mock()
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
            mock.Mock(),
            None,
            mock.Mock(),
            mock.Mock(),
            request,
            SendError(TOKEN_EXPIRED_FOLDER_MESSAGE),
            None,
        )

        toast = show_error_toast.call_args.args[1]
        self.assertIn("Hello", toast)
        self.assertIn("Sign-in expired", toast)
        self.assertIn("still in Outbox", toast)

    @mock.patch("post.compose_window.park_outbound_message")
    @mock.patch("post.compose_window.show_error_toast")
    def test_finish_validation_error_parks_in_outbox(
        self, show_error_toast, park
    ) -> None:
        parent = mock.Mock()
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
            mock.Mock(),
            None,
            mock.Mock(),
            mock.Mock(),
            request,
            SendError("Subject must not contain line breaks."),
            None,
        )

        show_error_toast.assert_called_once_with(
            parent,
            "“Hello” could not be sent. The message is still in Outbox. "
            "Subject must not contain line breaks.",
        )
        park.assert_called_once_with("queue-1", "Subject must not contain line breaks.")

    def test_finish_success_none_defaults_to_message_sent(self) -> None:
        set_status = mock.Mock()
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
            None,
            set_status,
            None,
            mock.Mock(),
            mock.Mock(),
            request,
            None,
            None,
        )

        set_status.assert_called_once_with("Message sent")

    def test_finish_success_empty_status_clears_hint(self) -> None:
        set_status = mock.Mock()
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
            None,
            set_status,
            None,
            mock.Mock(),
            mock.Mock(),
            request,
            None,
            "",
        )

        set_status.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()
