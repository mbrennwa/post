# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-account Camel helper IPC and supervisor (#437)."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from post.mail.camel_ipc import read_message, write_message
from post.mail.camel_runtime import (
    AccountCamelRuntime,
    CamelHelperTimeout,
    CamelRuntimePool,
    camel_helpers_enabled,
)

_FAKE_HELPER = textwrap.dedent(
    """
    import os, sys, time
    from post.mail.camel_ipc import read_message, write_message

    account = sys.argv[1] if len(sys.argv) > 1 else "acct"
    stdout = sys.stdout.buffer
    stdin = sys.stdin.buffer
    write_message(stdout, {"type": "ready", "account_uid": account, "pid": os.getpid()})
    while True:
        msg = read_message(stdin)
        if msg is None:
            break
        if msg.get("type") == "shutdown":
            write_message(stdout, {"type": "bye", "id": msg.get("id")})
            break
        if msg.get("type") != "call":
            continue
        method = msg.get("method")
        args = msg.get("args") or []
        if method == "ping":
            write_message(
                stdout,
                {
                    "type": "result",
                    "id": msg.get("id"),
                    "ok": True,
                    "result": {"ok": True, "pid": os.getpid(), "account_uid": account},
                },
            )
        elif method == "sleep_for_test":
            time.sleep(float(args[0]) if args else 0)
            write_message(
                stdout,
                {
                    "type": "result",
                    "id": msg.get("id"),
                    "ok": True,
                    "result": {"slept": args[0] if args else 0},
                },
            )
        else:
            write_message(
                stdout,
                {
                    "type": "result",
                    "id": msg.get("id"),
                    "ok": False,
                    "error": f"unknown {method}",
                    "error_type": "ValueError",
                },
            )
    """
)


class CamelIpcTests(unittest.TestCase):
    def test_roundtrip_bytes(self) -> None:
        import io

        buf = io.BytesIO()
        write_message(buf, {"type": "result", "ok": True, "result": b"hi"})
        buf.seek(0)
        msg = read_message(buf)
        assert msg is not None
        self.assertEqual(msg["result"], b"hi")


class CamelHelpersEnabledTests(unittest.TestCase):
    def test_disabled_in_helper_process(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"POST_MAIL_CAMEL_HELPER_PROCESS": "1", "POST_MAIL_CAMEL_HELPERS": "1"},
        ):
            self.assertFalse(camel_helpers_enabled())


class FakeHelperRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._script = Path(self._tmpdir.name) / "fake_helper.py"
        self._script.write_text(_FAKE_HELPER, encoding="utf-8")
        self._env = os.environ.copy()
        src = str(Path(__file__).resolve().parents[1] / "src")
        self._env["PYTHONPATH"] = src + os.pathsep + self._env.get("PYTHONPATH", "")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _patch_runtime(self, runtime: AccountCamelRuntime) -> AccountCamelRuntime:
        account_uid = runtime.account_uid
        script = str(self._script)
        env = self._env

        def _cmd() -> list[str]:
            return [sys.executable, script, account_uid]

        runtime._helper_command = _cmd  # type: ignore[method-assign]
        original_start = runtime._start_unlocked

        def _start() -> None:
            with mock.patch.dict(os.environ, env, clear=False):
                original_start()

        runtime._start_unlocked = _start  # type: ignore[method-assign]
        return runtime

    def test_ping_roundtrip(self) -> None:
        runtime = self._patch_runtime(AccountCamelRuntime(account_uid="acct-a"))
        try:
            result = runtime.call("ping", ["acct-a"], timeout=10.0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["account_uid"], "acct-a")
            self.assertIsInstance(result["pid"], int)
        finally:
            runtime.kill()

    def test_timeout_kills_helper(self) -> None:
        runtime = self._patch_runtime(AccountCamelRuntime(account_uid="acct-a"))
        try:
            with self.assertRaises(CamelHelperTimeout):
                runtime.call("sleep_for_test", [5.0], timeout=0.3)
            self.assertTrue(runtime._proc is None or runtime._proc.poll() is not None)
        finally:
            runtime.kill()

    def test_two_accounts_isolated(self) -> None:
        """Killing account A must not prevent account B from answering."""
        a = self._patch_runtime(AccountCamelRuntime(account_uid="acct-a"))
        b = self._patch_runtime(AccountCamelRuntime(account_uid="acct-b"))
        try:
            self.assertTrue(a.call("ping", ["acct-a"], timeout=10.0)["ok"])
            self.assertTrue(b.call("ping", ["acct-b"], timeout=10.0)["ok"])
            with self.assertRaises(CamelHelperTimeout):
                a.call("sleep_for_test", [5.0], timeout=0.2)
            result = b.call("ping", ["acct-b"], timeout=10.0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["account_uid"], "acct-b")
        finally:
            a.kill()
            b.kill()

    def test_pool_kill_account(self) -> None:
        pool = CamelRuntimePool()
        self._patch_runtime(pool.get("acct-a"))
        self._patch_runtime(pool.get("acct-b"))
        try:
            self.assertTrue(pool.call("acct-a", "ping", ["acct-a"], timeout=10.0)["ok"])
            self.assertTrue(pool.call("acct-b", "ping", ["acct-b"], timeout=10.0)["ok"])
            pool.kill_account("acct-a")
            self.assertTrue(pool.call("acct-b", "ping", ["acct-b"], timeout=10.0)["ok"])
        finally:
            pool.shutdown_all()
