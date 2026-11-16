# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""#537: size-aware message fetch, background preemption, honest error text."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from post.mail import fetch_policy
from post.mail.eds import (
    CANCEL_SCOPE_BACKGROUND,
    HELPER_OP_BACKGROUND,
    HELPER_OP_READ,
    MailService,
    MessageNotAvailableError,
    MessageUnavailableReason,
)
from post.mail.network_errors import (
    MESSAGE_READ_TIMEOUT_MESSAGE,
    OFFLINE_FOLDER_MESSAGE,
    OFFLINE_MESSAGE_READ_MESSAGE,
    SERVER_TIMEOUT_FOLDER_MESSAGE,
    format_folder_load_error,
    format_message_read_error,
    is_connectivity_error,
    is_timeout_error,
)
from post.mail.offline_settings import downsync_expression_for_mode
from post.preferences import OFFLINE_BODY_SYNC_ALL

MB = 1024 * 1024


class FetchPolicyTests(unittest.TestCase):
    def test_unknown_or_empty_size_keeps_base_timeout(self) -> None:
        self.assertEqual(fetch_policy.fetch_timeout_seconds(None), 30.0)
        self.assertEqual(fetch_policy.fetch_timeout_seconds(0), 30.0)
        self.assertEqual(fetch_policy.fetch_timeout_seconds(-5), 30.0)

    def test_small_message_stays_close_to_base(self) -> None:
        self.assertLess(fetch_policy.fetch_timeout_seconds(50 * 1024), 31.0)

    def test_timeout_grows_with_size(self) -> None:
        small = fetch_policy.fetch_timeout_seconds(1 * MB)
        large = fetch_policy.fetch_timeout_seconds(33 * MB)
        self.assertGreater(large, small)
        # The reported 33.8 MB message must get far more than the old 30 s.
        self.assertGreater(fetch_policy.fetch_timeout_seconds(33_834_468), 200.0)

    def test_timeout_is_capped(self) -> None:
        self.assertEqual(
            fetch_policy.fetch_timeout_seconds(10_000 * MB),
            fetch_policy.MESSAGE_READ_MAX_SECONDS,
        )

    def test_background_limit(self) -> None:
        self.assertTrue(fetch_policy.background_fetch_allowed(None))
        self.assertTrue(fetch_policy.background_fetch_allowed(5 * MB))
        self.assertFalse(fetch_policy.background_fetch_allowed(33_834_468))

    def test_download_note_only_for_long_transfers(self) -> None:
        self.assertIsNone(fetch_policy.download_note(None))
        self.assertIsNone(fetch_policy.download_note(100 * 1024))
        self.assertEqual(
            fetch_policy.download_note(33_834_468), "Downloading 32.3 MB…"
        )


class DownsyncExpressionTests(unittest.TestCase):
    def test_expression_excludes_oversized_messages(self) -> None:
        expression = downsync_expression_for_mode(OFFLINE_BODY_SYNC_ALL)
        limit_kb = fetch_policy.BACKGROUND_FETCH_MAX_BYTES // 1024
        self.assertEqual(expression, f"(match-all (< (get-size) {limit_kb}))")


class ErrorClassificationTests(unittest.TestCase):
    def test_timeout_is_not_offline(self) -> None:
        for exc in (
            TimeoutError("Timed out loading message 1"),
            RuntimeError("operation timed out"),
            RuntimeError("I/O error"),
            RuntimeError("Connection reset by peer"),
        ):
            with self.subTest(exc=exc):
                self.assertTrue(is_timeout_error(exc))
                self.assertFalse(is_connectivity_error(exc))

    def test_unreachable_server_is_connectivity(self) -> None:
        for exc in (
            RuntimeError("Network is unreachable"),
            RuntimeError("Error resolving 'imap.gmail.com'"),
            RuntimeError("Service must be working online"),
        ):
            with self.subTest(exc=exc):
                self.assertTrue(is_connectivity_error(exc))
                self.assertFalse(is_timeout_error(exc))

    def test_message_timeout_never_says_offline_or_folders(self) -> None:
        text = format_message_read_error(
            TimeoutError("Timed out loading message 1"), network_available=True
        )
        self.assertEqual(text, MESSAGE_READ_TIMEOUT_MESSAGE)
        self.assertNotIn("Offline", text)
        self.assertNotIn("older", text)

    def test_message_read_never_mentions_folders(self) -> None:
        for exc in (
            RuntimeError("Network is unreachable"),
            TimeoutError("x"),
            RuntimeError("boom"),
        ):
            for available in (True, False, None):
                with self.subTest(exc=exc, available=available):
                    text = format_message_read_error(
                        exc, network_available=available
                    )
                    self.assertNotIn("folders", text.lower())

    def test_offline_text_follows_real_network_state(self) -> None:
        self.assertEqual(
            format_message_read_error(
                RuntimeError("boom"), network_available=False
            ),
            OFFLINE_MESSAGE_READ_MESSAGE,
        )
        self.assertEqual(
            format_message_read_error(
                RuntimeError("Network is unreachable"), network_available=True
            ),
            OFFLINE_MESSAGE_READ_MESSAGE,
        )

    def test_typed_message_error_uses_its_own_text(self) -> None:
        error = MessageNotAvailableError(
            "1",
            "INBOX",
            reason=MessageUnavailableReason.READ_TIMEOUT,
            size_bytes=33_834_468,
        )
        text = format_message_read_error(error)
        self.assertIn("32.3 MB", text)
        self.assertNotIn("Offline", text)

    def test_folder_timeout_is_not_offline(self) -> None:
        self.assertEqual(
            format_folder_load_error(TimeoutError("x"), network_available=True),
            SERVER_TIMEOUT_FOLDER_MESSAGE,
        )
        self.assertEqual(
            format_folder_load_error(
                RuntimeError("Network is unreachable"), network_available=True
            ),
            OFFLINE_FOLDER_MESSAGE,
        )
        self.assertEqual(
            format_folder_load_error(RuntimeError("boom"), network_available=False),
            OFFLINE_FOLDER_MESSAGE,
        )


class CancelScopeTests(unittest.TestCase):
    def _mail(self) -> MailService:
        return MailService(registry=mock.Mock())

    def test_background_scope_spares_a_user_read(self) -> None:
        mail = self._mail()
        cancellable = Gio.Cancellable()
        mail._register_helper_op_cancellable(cancellable, HELPER_OP_READ)
        mail.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
        self.assertFalse(cancellable.is_cancelled())

    def test_background_scope_cancels_background_op(self) -> None:
        mail = self._mail()
        cancellable = Gio.Cancellable()
        mail._register_helper_op_cancellable(cancellable, HELPER_OP_BACKGROUND)
        mail.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
        self.assertTrue(cancellable.is_cancelled())

    def test_all_scope_cancels_a_read(self) -> None:
        mail = self._mail()
        cancellable = Gio.Cancellable()
        mail._register_helper_op_cancellable(cancellable, HELPER_OP_READ)
        mail.cancel_helper_op()
        self.assertTrue(cancellable.is_cancelled())

    def test_kind_resets_after_clear(self) -> None:
        mail = self._mail()
        first = Gio.Cancellable()
        mail._register_helper_op_cancellable(first, HELPER_OP_READ)
        mail._clear_helper_op_cancellable(first)
        second = Gio.Cancellable()
        mail._register_helper_op_cancellable(second)
        mail.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
        self.assertTrue(second.is_cancelled())

    def test_preempt_background_op_uses_background_scope(self) -> None:
        mail = MailService.__new__(MailService)
        mail.preempt_account_helper_op = mock.Mock()
        mail.preempt_background_op("acct")
        mail.preempt_account_helper_op.assert_called_once_with(
            "acct", CANCEL_SCOPE_BACKGROUND
        )

    def test_foreground_job_preempts_background(self) -> None:
        mail = self._mail()
        mail.preempt_background_op = mock.Mock()
        mail._camel_pool = mock.Mock()
        with mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True):
            mail.submit_job(
                "folder_message_sync",
                lambda: None,
                epic_lane="foreground",
                account_uid="acct",
            )
            mail.preempt_background_op.assert_called_once_with("acct")
            mail.preempt_background_op.reset_mock()
            mail.submit_job(
                "maintenance",
                lambda: None,
                epic_lane="background",
                account_uid="acct",
            )
            mail.preempt_background_op.assert_not_called()


class ReadTimeoutTests(unittest.TestCase):
    @staticmethod
    def _cancelled_error() -> GLib.Error:
        return GLib.Error.new_literal(
            Gio.io_error_quark(),
            "Operation was cancelled",
            Gio.IOErrorEnum.CANCELLED,
        )

    def _folder(self, started: threading.Event):
        folder = mock.Mock()

        def blocking_get(_uid: str, cancel: Gio.Cancellable):
            started.set()
            self.assertTrue(
                self._wait_cancelled(cancel), "read was never cancelled"
            )
            raise self._cancelled_error()

        folder.get_message_sync.side_effect = blocking_get
        return folder

    @staticmethod
    def _wait_cancelled(cancel: Gio.Cancellable) -> bool:
        deadline = threading.Event()
        for _ in range(200):
            if cancel.is_cancelled():
                return True
            deadline.wait(0.02)
        return False

    def test_expired_limit_raises_read_timeout(self) -> None:
        mail = MailService(registry=mock.Mock())
        folder = self._folder(threading.Event())
        with mock.patch("post.mail.eds.fetch_timeout_seconds", return_value=0.05):
            with mock.patch.object(mail, "_try_message_cached", return_value=None):
                with self.assertRaises(MessageNotAvailableError) as ctx:
                    mail._get_message_sync_with_timeout(
                        folder,
                        "1",
                        message_uid="1",
                        folder_name="INBOX",
                        size_bytes=33_834_468,
                    )
        self.assertEqual(ctx.exception.reason, MessageUnavailableReason.READ_TIMEOUT)
        self.assertEqual(ctx.exception.size_bytes, 33_834_468)

    def test_cancel_by_newer_read_raises_superseded(self) -> None:
        mail = MailService(registry=mock.Mock())
        started = threading.Event()
        folder = self._folder(started)

        def cancel_soon() -> None:
            self.assertTrue(started.wait(timeout=2.0))
            mail.cancel_helper_op()

        thread = threading.Thread(target=cancel_soon, daemon=True)
        thread.start()
        with mock.patch.object(mail, "_try_message_cached", return_value=None):
            with self.assertRaises(MessageNotAvailableError) as ctx:
                mail._get_message_sync_with_timeout(
                    folder, "1", message_uid="1", folder_name="INBOX"
                )
        thread.join(timeout=2.0)
        self.assertEqual(ctx.exception.reason, MessageUnavailableReason.SUPERSEDED)

    def test_background_scope_does_not_cancel_running_read(self) -> None:
        mail = MailService(registry=mock.Mock())
        started = threading.Event()
        release = threading.Event()
        folder = mock.Mock()
        folder.get_message_sync.side_effect = lambda *_a: (
            started.set(),
            release.wait(2.0),
            "mime",
        )[-1]
        result: dict[str, object] = {}

        def run() -> None:
            result["mime"] = mail._get_message_sync_with_timeout(
                folder, "1", message_uid="1", folder_name="INBOX"
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=2.0))
        mail.cancel_helper_op(CANCEL_SCOPE_BACKGROUND)
        release.set()
        thread.join(timeout=2.0)
        self.assertEqual(result.get("mime"), "mime")

    def test_ipc_error_payload_carries_size(self) -> None:
        from post.mail.camel_helper import _error_payload

        error = MessageNotAvailableError(
            "1",
            "INBOX",
            reason=MessageUnavailableReason.READ_TIMEOUT,
            size_bytes=123,
        )
        payload = _error_payload("req", error)
        self.assertEqual(payload["error_details"]["size_bytes"], 123)
        self.assertEqual(
            payload["error_details"]["reason"], MessageUnavailableReason.READ_TIMEOUT
        )


class DownloadNoticeTests(unittest.TestCase):
    """The reader says "Downloading…" only when Camel has no local copy."""

    def setUp(self) -> None:
        self.mail = MailService(registry=mock.Mock())
        self.events: list[tuple] = []
        self.mail.set_message_download_listener(
            lambda *args: self.events.append(args)
        )

    def tearDown(self) -> None:
        from post.mail.eds import set_helper_event_sink

        set_helper_event_sink(None)

    def test_cached_message_does_not_announce_a_download(self) -> None:
        folder = mock.Mock()
        with mock.patch("post.mail.eds.get_account_user_online", return_value=True):
            with mock.patch.object(
                self.mail, "_try_message_cached", return_value="mime"
            ):
                with mock.patch.object(
                    self.mail, "_get_message_sync_with_timeout"
                ) as fetch:
                    result = self.mail._get_message_mime_sync(
                        folder, "acct", "INBOX", "54722"
                    )
        self.assertEqual(result, "mime")
        fetch.assert_not_called()
        self.assertEqual(self.events, [])

    def test_uncached_message_announces_download_with_size(self) -> None:
        folder = mock.Mock()
        folder.get_message_sync.return_value = "mime"
        self.mail._get_message_sync_with_timeout(
            folder,
            "54722",
            account_uid="acct",
            message_uid="54722",
            folder_name="INBOX",
            size_bytes=33_834_468,
        )
        self.assertEqual(self.events, [("acct", "INBOX", "54722", 33_834_468)])

    def test_helper_process_sends_event_over_ipc_instead(self) -> None:
        from post.mail.eds import set_helper_event_sink

        sent: list[dict] = []
        set_helper_event_sink(sent.append)
        self.mail._emit_message_download("acct", "INBOX", "54722", 123)
        self.assertEqual(self.events, [])
        self.assertEqual(sent[0]["event"], "message_download")
        self.assertEqual(sent[0]["message_uid"], "54722")
        self.assertEqual(sent[0]["size_bytes"], 123)

    def test_ui_forwards_helper_event_to_listener(self) -> None:
        self.mail._on_helper_event(
            "acct",
            {
                "type": "event",
                "event": "message_download",
                "folder_name": "INBOX",
                "message_uid": "54722",
                "size_bytes": 456,
            },
        )
        self.mail._on_helper_event("acct", {"type": "event", "event": "other"})
        self.assertEqual(self.events, [("acct", "INBOX", "54722", 456)])

    def test_runtime_reader_dispatches_events(self) -> None:
        from post.mail.camel_runtime import AccountCamelRuntime

        runtime = AccountCamelRuntime(account_uid="acct")
        got: list[tuple[str, dict]] = []
        runtime.set_event_callback(lambda uid, msg: got.append((uid, msg)))
        runtime._dispatch_event({"type": "event", "event": "message_download"})
        self.assertEqual(got[0][0], "acct")

    def test_ipc_frames_stay_intact_under_concurrent_writes(self) -> None:
        import io

        from post.mail.camel_ipc import read_message, write_message

        buffer = io.BytesIO()

        def writer(tag: str) -> None:
            for index in range(200):
                write_message(buffer, {"tag": tag, "n": index, "pad": "x" * 500})

        threads = [
            threading.Thread(target=writer, args=(tag,)) for tag in ("a", "b", "c")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        buffer.seek(0)
        count = 0
        while read_message(buffer) is not None:
            count += 1
        self.assertEqual(count, 600)


class HelperCancelScopeTests(unittest.TestCase):
    def test_helper_passes_scope_to_cancel_helper_op(self) -> None:
        from post.mail.camel_helper import _cancel_helper_op

        mail = mock.Mock()
        _cancel_helper_op(mail, {"type": "cancel", "scope": "background"})
        mail.cancel_helper_op.assert_called_once_with("background")

    def test_helper_defaults_to_all_for_old_cancel_messages(self) -> None:
        from post.mail.camel_helper import _cancel_helper_op

        mail = mock.Mock()
        _cancel_helper_op(mail, {"type": "cancel"})
        mail.cancel_helper_op.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
