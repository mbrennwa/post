# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from post.mail.eds import (
    EDS_REGISTRY_CONNECT_ERROR,
    EDS_REGISTRY_RECONNECT_AFTER_LOCAL_ERROR,
    EDS_REGISTRY_RECONNECT_ERROR,
    MailService,
    source_registry_new_sync,
)


def _timed_out_error() -> GLib.Error:
    return GLib.Error.new_literal(
        Gio.io_error_quark(),
        "Timeout was reached",
        int(Gio.IOErrorEnum.TIMED_OUT),
    )


class SourceRegistryNewSyncTests(unittest.TestCase):
    @patch("post.mail.eds.EDataServer.SourceRegistry.new_sync")
    def test_maps_timeout_to_runtime_error(self, new_sync: MagicMock) -> None:
        new_sync.side_effect = _timed_out_error()
        with self.assertRaises(RuntimeError) as ctx:
            source_registry_new_sync()
        self.assertEqual(str(ctx.exception), EDS_REGISTRY_CONNECT_ERROR)
        self.assertIsInstance(ctx.exception.__cause__, GLib.Error)

    @patch("post.mail.eds.EDataServer.SourceRegistry.new_sync")
    def test_maps_none_to_runtime_error(self, new_sync: MagicMock) -> None:
        new_sync.return_value = None
        with self.assertRaises(RuntimeError) as ctx:
            source_registry_new_sync(failure_message=EDS_REGISTRY_RECONNECT_ERROR)
        self.assertEqual(str(ctx.exception), EDS_REGISTRY_RECONNECT_ERROR)

    @patch("post.mail.eds.EDataServer.SourceRegistry.new_sync")
    def test_returns_registry(self, new_sync: MagicMock) -> None:
        registry = MagicMock(name="registry")
        new_sync.return_value = registry
        self.assertIs(source_registry_new_sync(), registry)


class MailServiceConnectRegistryTests(unittest.TestCase):
    @patch("post.mail.eds.ensure_post_local_mail_transport")
    @patch("post.mail.eds.source_registry_new_sync")
    def test_connect_propagates_registry_failure(
        self,
        new_sync: MagicMock,
        _ensure_local: MagicMock,
    ) -> None:
        new_sync.side_effect = RuntimeError(EDS_REGISTRY_CONNECT_ERROR)
        with self.assertRaises(RuntimeError) as ctx:
            MailService.connect()
        self.assertEqual(str(ctx.exception), EDS_REGISTRY_CONNECT_ERROR)
        new_sync.assert_called_once_with(failure_message=EDS_REGISTRY_CONNECT_ERROR)

    @patch("post.mail.eds.MailService._drop_orphan_account_caches")
    @patch("post.mail.eds.MailService._ensure_mail_io_callbacks")
    @patch("post.mail.eds.ensure_post_local_mail_transport")
    @patch("post.mail.eds.source_registry_new_sync")
    def test_connect_second_new_sync_uses_after_local_message(
        self,
        new_sync: MagicMock,
        _ensure_local: MagicMock,
        _callbacks: MagicMock,
        _gc: MagicMock,
    ) -> None:
        registry = MagicMock(name="registry")
        new_sync.side_effect = [
            registry,
            RuntimeError(EDS_REGISTRY_RECONNECT_AFTER_LOCAL_ERROR),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            MailService.connect()
        self.assertEqual(str(ctx.exception), EDS_REGISTRY_RECONNECT_AFTER_LOCAL_ERROR)
        self.assertEqual(
            [call.kwargs.get("failure_message") for call in new_sync.call_args_list],
            [
                EDS_REGISTRY_CONNECT_ERROR,
                EDS_REGISTRY_RECONNECT_AFTER_LOCAL_ERROR,
            ],
        )


if __name__ == "__main__":
    unittest.main()
