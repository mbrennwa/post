# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""A cached message opens without the helper's call lock (#549)."""

from __future__ import annotations

import io
import threading
import unittest
from unittest import mock

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")
from gi.repository import Camel, GLib, Gio

from post.mail import network_errors
from post.mail.camel_ipc import read_message
from post.mail.camel_runtime import CamelHelperBusy, CamelHelperError
from post.mail.eds import (
    MailService,
    MessageNotAvailableError,
    MessageUnavailableReason,
)

_RAW = (
    b"From: Ann <ann@example.org>\r\n"
    b"To: bob@example.org\r\n"
    b"Subject: Cached hello\r\n"
    b"Message-ID: <cached-1@example.org>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"cached body\r\n"
)


def _mime() -> Camel.MimeMessage:
    message = Camel.MimeMessage()
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(_RAW))
    message.construct_from_input_stream_sync(stream, None)
    return message


def _info(*, seen: bool) -> mock.Mock:
    info = mock.Mock()
    info.get_flags.return_value = (
        Camel.MessageFlags.SEEN if seen else Camel.MessageFlags(0)
    )
    return info


class ReadCachedMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        self.folder = mock.Mock()
        store = mock.Mock()
        self.service._stores["acct"] = store
        self.service._open_folders[("acct", "INBOX")] = (store, self.folder)

    def _read(self, *, seen: bool = False, mark_seen: bool = True):
        with (
            mock.patch.object(
                self.service, "_try_message_cached", return_value=_mime()
            ),
            mock.patch(
                "post.mail.eds.folder_get_message_info",
                return_value=_info(seen=seen),
            ),
            mock.patch(
                "post.mail.eds.message_info_to_dict",
                return_value={"uid": "7", "flags": {"seen": seen}},
            ),
        ):
            return self.service.read_cached_message(
                "acct", "INBOX", "7", mark_seen=mark_seen
            )

    def test_returns_the_message_from_the_local_copy(self) -> None:
        result = self._read()
        assert result is not None
        self.assertEqual(result["uid"], "7")
        self.assertEqual(result["subject"], "Cached hello")
        self.assertIn("cached body", result["body_plain"])

    def test_unread_message_asks_the_ui_to_mark_it_seen(self) -> None:
        result = self._read(seen=False)
        assert result is not None
        self.assertTrue(result["needs_mark_seen"])
        self.assertFalse(result["flags"]["seen"])

    def test_no_mark_seen_when_not_requested_or_already_seen(self) -> None:
        for kwargs in ({"seen": False, "mark_seen": False}, {"seen": True}):
            result = self._read(**kwargs)
            assert result is not None
            self.assertNotIn("needs_mark_seen", result)

    def test_matches_the_serialized_read(self) -> None:
        """Both paths build the reader dict from the same function."""
        fast = self._read(seen=True)
        service = self.service
        with (
            mock.patch.object(service, "_allow_network_unlocked", return_value=False),
            mock.patch.object(
                service,
                "_open_folder_for_message_read_unlocked",
                return_value=self.folder,
            ),
            mock.patch.object(
                service, "account_may_contact_server", return_value=False
            ),
            mock.patch.object(
                service, "_get_message_mime_sync", return_value=_mime()
            ),
            mock.patch(
                "post.mail.eds.folder_get_message_info",
                return_value=_info(seen=True),
            ),
            mock.patch(
                "post.mail.eds.message_info_to_dict",
                return_value={"uid": "7", "flags": {"seen": True}},
            ),
        ):
            service._recovered_read_uid = None
            slow = service._read_message_unlocked("acct", "INBOX", "7")
        self.assertEqual(fast, slow)

    def test_never_touches_the_server_or_the_mail_thread(self) -> None:
        self.folder.get_message_sync.side_effect = AssertionError("server")
        self.folder.synchronize_message_sync.side_effect = AssertionError("sync")
        with (
            mock.patch(
                "post.mail.eds.run_on_mail_thread",
                side_effect=AssertionError("mail thread"),
            ),
            mock.patch.object(
                self.service,
                "_open_folder_unlocked",
                side_effect=AssertionError("open folder"),
            ),
            mock.patch.object(
                self.service,
                "_get_store_unlocked",
                side_effect=AssertionError("store"),
            ),
        ):
            self.assertIsNotNone(self._read())

    def test_miss_when_the_folder_was_never_opened(self) -> None:
        self.service._open_folders.clear()
        self.assertIsNone(self.service.read_cached_message("acct", "INBOX", "7"))

    def test_miss_when_there_is_no_local_copy(self) -> None:
        with mock.patch.object(
            self.service, "_try_message_cached", return_value=None
        ):
            self.assertIsNone(
                self.service.read_cached_message("acct", "INBOX", "7")
            )

    def test_stale_store_drops_the_folder_handle(self) -> None:
        self.service._stores["acct"] = mock.Mock()
        self.assertIsNone(self.service.read_cached_message("acct", "INBOX", "7"))
        self.assertNotIn(("acct", "INBOX"), self.service._open_folders)

    def test_opening_a_folder_registers_it(self) -> None:
        service = MailService(registry=mock.Mock())
        store = mock.Mock()
        folder = mock.Mock()
        service._stores["acct"] = store
        with (
            mock.patch.object(service, "_get_store_unlocked", return_value=store),
            mock.patch.object(
                service, "_get_named_folder_unlocked", return_value=folder
            ),
        ):
            self.assertIs(service._open_folder_unlocked("acct", "INBOX"), folder)
        self.assertIs(service._registered_open_folder("acct", "INBOX"), folder)


class ReadMessageFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        patcher = mock.patch("post.mail.eds.camel_helpers_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = mock.Mock()
        self.service._camel_pool = self.pool

    def test_cached_read_skips_the_helper_call(self) -> None:
        self.pool.peek_cached_message.return_value = {"uid": "7"}
        with mock.patch.object(self.service, "_camel_helper_call") as call:
            result = self.service.read_message("acct", "INBOX", "7")
        self.assertEqual(result, {"uid": "7"})
        call.assert_not_called()
        self.pool.peek_cached_message.assert_called_once_with(
            "acct", "INBOX", "7", mark_seen=True
        )

    def test_miss_takes_the_serialized_read(self) -> None:
        self.pool.peek_cached_message.return_value = None
        with mock.patch.object(
            self.service, "_camel_helper_call", return_value={"uid": "7", "slow": 1}
        ) as call:
            result = self.service.read_message("acct", "INBOX", "7")
        self.assertEqual(result["slow"], 1)
        self.assertEqual(call.call_args.args[0], "read_message")

    def test_helper_error_takes_the_serialized_read(self) -> None:
        self.pool.peek_cached_message.side_effect = CamelHelperError("gone")
        with mock.patch.object(
            self.service, "_camel_helper_call", return_value={"uid": "7"}
        ) as call:
            self.service.read_message("acct", "INBOX", "7")
        call.assert_called_once()


class HelperBusyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MailService(registry=mock.Mock())
        self.pool = mock.Mock()
        self.service._camel_pool = self.pool

    def test_busy_read_is_not_reported_as_a_server_timeout(self) -> None:
        self.pool.call.side_effect = CamelHelperBusy("busy")
        with self.assertRaises(MessageNotAvailableError) as raised:
            self.service._camel_helper_call_once(
                "read_message", "acct", ["acct", "INBOX", "7"], {}
            )
        self.assertEqual(raised.exception.reason, MessageUnavailableReason.HELPER_BUSY)
        self.assertEqual(raised.exception.message_uid, "7")
        self.assertEqual(
            raised.exception.user_message(), network_errors.HELPER_BUSY_MESSAGE
        )
        self.assertNotIn("server", raised.exception.user_message().lower())

    def test_busy_for_other_methods_stays_busy(self) -> None:
        self.pool.call.side_effect = CamelHelperBusy("busy")
        with self.assertRaises(CamelHelperBusy):
            self.service._camel_helper_call_once(
                "read_attachment_data", "acct", ["acct", "INBOX", "7", 0], {}
            )

    def test_read_waits_up_to_the_message_read_limit(self) -> None:
        self.pool.call.return_value = {"uid": "7"}
        self.service._camel_helper_call(
            "read_message", "acct", ["acct", "INBOX", "7"], {}
        )
        self.assertEqual(self.pool.call.call_args.kwargs["wait_timeout"], 30.0)

    def test_a_long_wait_notifies_the_reader(self) -> None:
        seen: list[tuple[str, str, str]] = []
        self.service.set_message_wait_listener(lambda *key: seen.append(key))

        def _call(*_args, on_wait=None, **_kwargs):
            on_wait("flush_next_account_operation", 3.0)
            return {"uid": "7"}

        self.pool.call.side_effect = _call
        self.service._camel_helper_call(
            "read_message", "acct", ["acct", "INBOX", "7"], {}
        )
        self.assertEqual(seen, [("acct", "INBOX", "7")])

    def test_only_reads_notify(self) -> None:
        seen: list[tuple[str, str, str]] = []
        self.service.set_message_wait_listener(lambda *key: seen.append(key))
        self.pool.call.return_value = None
        self.service._camel_helper_call("list_folders", "acct", ["acct"], {})
        self.assertIsNone(self.pool.call.call_args.kwargs["on_wait"])
        self.assertEqual(seen, [])


class BusyErrorTextTests(unittest.TestCase):
    def test_lock_wait_is_not_a_timeout(self) -> None:
        exc = CamelHelperBusy(
            "gave up waiting for camel helper lock account=a method=read_message"
        )
        self.assertFalse(network_errors.is_timeout_error(exc))
        self.assertFalse(network_errors.is_connectivity_error(exc))
        self.assertEqual(
            network_errors.format_message_read_error(exc),
            network_errors.HELPER_BUSY_MESSAGE,
        )
        self.assertEqual(
            network_errors.format_attachment_error(exc),
            network_errors.HELPER_BUSY_MESSAGE,
        )

    def test_busy_text_does_not_claim_the_server_is_slow(self) -> None:
        text = network_errors.HELPER_BUSY_MESSAGE.lower()
        self.assertNotIn("did not respond", text)
        self.assertNotIn("server", text)

    def test_a_real_server_timeout_is_still_a_timeout(self) -> None:
        self.assertTrue(network_errors.is_timeout_error(TimeoutError("slow")))
        self.assertEqual(
            network_errors.format_message_read_error(TimeoutError("slow")),
            network_errors.MESSAGE_READ_TIMEOUT_MESSAGE,
        )


class HelperSideChannelTests(unittest.TestCase):
    def _answer(self, mail: mock.Mock) -> dict:
        from post.mail.camel_helper import _write_cached_read

        stdout = io.BytesIO()
        done = threading.Event()
        original_start = threading.Thread.start

        def _start(thread):
            original_start(thread)
            thread.join(timeout=5.0)
            done.set()

        with mock.patch.object(threading.Thread, "start", _start):
            _write_cached_read(
                mail,
                {
                    "id": "r1",
                    "folder_name": "INBOX",
                    "message_uid": "7",
                    "mark_seen": False,
                },
                stdout,
                "acct",
            )
        self.assertTrue(done.is_set())
        stdout.seek(0)
        reply = read_message(stdout)
        assert reply is not None
        return reply

    def test_answers_with_the_cached_message(self) -> None:
        mail = mock.Mock()
        mail.read_cached_message.return_value = {"uid": "7"}
        reply = self._answer(mail)
        self.assertEqual(reply["id"], "r1")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"], {"uid": "7"})
        mail.read_cached_message.assert_called_once_with(
            "acct", "INBOX", "7", mark_seen=False
        )

    def test_answers_none_for_a_miss(self) -> None:
        mail = mock.Mock()
        mail.read_cached_message.return_value = None
        reply = self._answer(mail)
        self.assertTrue(reply["ok"])
        self.assertIsNone(reply["result"])

    def test_failure_is_reported_not_raised(self) -> None:
        mail = mock.Mock()
        mail.read_cached_message.side_effect = RuntimeError("boom")
        reply = self._answer(mail)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["id"], "r1")


if __name__ == "__main__":
    unittest.main()
