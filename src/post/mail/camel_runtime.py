# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""UI-side supervisor for per-account Camel helper processes (#437 / #422)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .camel_ipc import read_message, write_message

log = logging.getLogger(__name__)

_HELPERS_ENV = "POST_MAIL_CAMEL_HELPERS"
_HELPER_PROCESS_ENV = "POST_MAIL_CAMEL_HELPER_PROCESS"
_DEFAULT_JOB_TIMEOUT = 120.0


def camel_helpers_enabled() -> bool:
    """True when the UI should spawn per-account Camel helpers.

    Disabled inside helper processes. **Default on** in the UI (#445): each
    helper uses private Camel data/cache dirs so processes do not share
    ``~/.local/share/evolution`` (the #439 mid-read failure mode). Opt out with
    ``POST_MAIL_CAMEL_HELPERS=0``.
    """
    if os.environ.get(_HELPER_PROCESS_ENV) == "1":
        return False
    raw = os.environ.get(_HELPERS_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class CamelHelperError(RuntimeError):
    """Raised when a helper call fails or the process is killed."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


class CamelHelperTimeout(CamelHelperError):
    """Raised when a helper job exceeds the watchdog timeout."""


@dataclass
class AccountCamelRuntime:
    """One helper child for a single ``account_uid``."""

    account_uid: str
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _serial: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _pending: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _reader_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )
    _alive: bool = field(default=False, init=False)
    _on_died: Callable[[str], None] | None = field(default=None, init=False, repr=False)

    def set_died_callback(self, callback: Callable[[str], None] | None) -> None:
        self._on_died = callback

    def ensure_started(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._alive:
                return
            self._start_unlocked()

    def _helper_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "post.mail.camel_helper",
            "--account-uid",
            self.account_uid,
        ]

    def _start_unlocked(self) -> None:
        # Only SIGKILL a still-living child. Reaping an already-dead process
        # avoids exit_code=-9 storms when retries race (#445).
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._stop_unlocked(kill=True)
        else:
            self._reap_dead_unlocked()
        env = os.environ.copy()
        env[_HELPER_PROCESS_ENV] = "1"
        env[_HELPERS_ENV] = "0"
        # Ensure src layout works the same as ``python3 -m post.main``.
        cmd = self._helper_command()
        log.info(
            "spawning camel helper account=%s cmd=%s",
            self.account_uid,
            cmd,
        )
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
        self._alive = False
        self._pending.clear()
        assert self._proc.stdout is not None
        ready = read_message(self._proc.stdout)
        if ready is None or ready.get("type") != "ready":
            self._stop_unlocked(kill=True)
            raise CamelHelperError(
                f"camel helper for {self.account_uid} did not become ready"
            )
        self._alive = True
        self._reader_thread = threading.Thread(
            target=self._reader_main,
            name=f"camel-helper-reader-{self.account_uid[:8]}",
            daemon=True,
        )
        self._reader_thread.start()
        # Drain stderr so a chatty helper cannot block.
        threading.Thread(
            target=self._drain_stderr,
            name=f"camel-helper-stderr-{self.account_uid[:8]}",
            daemon=True,
        ).start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                # Promote helper diagnostics: crashes otherwise only show up as
                # "camel helper process exited" on the UI side (#437).
                log.warning("camel-helper[%s]: %s", self.account_uid[:8], text)
        except Exception:
            log.debug("stderr drain failed account=%s", self.account_uid, exc_info=True)

    def _reader_main(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        exit_code: int | None = None
        try:
            while True:
                msg = read_message(proc.stdout)
                if msg is None:
                    break
                if msg.get("type") != "result":
                    continue
                req_id = str(msg.get("id") or "")
                with self._lock:
                    pending = self._pending.pop(req_id, None)
                if pending is None:
                    continue
                pending["msg"] = msg
                pending["event"].set()
        except Exception:
            log.warning(
                "helper reader exited account=%s",
                self.account_uid,
                exc_info=True,
            )
        finally:
            if proc is not None:
                try:
                    exit_code = proc.wait(timeout=0.5)
                except Exception:
                    exit_code = proc.poll()
                # stdout EOF with poll() still None usually means a short IPC
                # read desync or a still-writing child — not a clean exit.
                if exit_code is None:
                    try:
                        os.kill(proc.pid, 0)
                        still_alive = True
                    except OSError:
                        still_alive = False
                    if still_alive:
                        log.warning(
                            "camel helper stdout closed while process still "
                            "alive account=%s pid=%s; killing to resync IPC",
                            self.account_uid,
                            proc.pid,
                        )
                        try:
                            proc.kill()
                            exit_code = proc.wait(timeout=2.0)
                        except Exception:
                            exit_code = proc.poll()
            log.warning(
                "camel helper reader stopped account=%s exit_code=%s",
                self.account_uid,
                exit_code,
            )
            with self._lock:
                # Ignore stale readers after kill/respawn (#445).
                if self._proc is not proc:
                    return
                self._alive = False
                for pending in self._pending.values():
                    pending["msg"] = {
                        "type": "result",
                        "ok": False,
                        "error": "camel helper process exited",
                        "error_type": "CamelHelperError",
                    }
                    pending["event"].set()
                self._pending.clear()
            callback = self._on_died
            if callback is not None:
                try:
                    callback(self.account_uid)
                except Exception:
                    log.debug("on_died callback failed", exc_info=True)

    def call(
        self,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        timeout: float | None = _DEFAULT_JOB_TIMEOUT,
    ) -> Any:
        """Run ``method`` in the helper; raise on failure / timeout / kill."""
        with self._serial:
            return self._call_unlocked(
                method, args or [], kwargs or {}, timeout=timeout
            )

    def _call_unlocked(
        self,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
        *,
        timeout: float | None,
    ) -> Any:
        self.ensure_started()
        req_id = uuid.uuid4().hex
        event = threading.Event()
        pending: dict[str, Any] = {"event": event, "msg": None}
        with self._lock:
            if self._proc is None or self._proc.stdin is None or not self._alive:
                raise CamelHelperError(f"camel helper not running for {self.account_uid}")
            self._pending[req_id] = pending
            write_message(
                self._proc.stdin,
                {
                    "type": "call",
                    "id": req_id,
                    "method": method,
                    "args": args,
                    "kwargs": kwargs,
                },
            )
        finished = event.wait(timeout=None if timeout is None else max(0.1, timeout))
        if not finished:
            log.warning(
                "camel helper timeout account=%s method=%s timeout=%s",
                self.account_uid,
                method,
                timeout,
            )
            self.kill()
            raise CamelHelperTimeout(
                f"camel helper timed out for {self.account_uid} method={method}"
            )
        msg = pending.get("msg") or {}
        if not msg.get("ok"):
            err = str(msg.get("error") or "camel helper call failed")
            err_type = str(msg.get("error_type") or "CamelHelperError")
            details = msg.get("error_details")
            if not isinstance(details, dict):
                details = None
            if err_type == "CamelHelperTimeout":
                raise CamelHelperTimeout(err, error_type=err_type, details=details)
            raise CamelHelperError(
                f"{err_type}: {err}",
                error_type=err_type,
                details=details,
            )
        return msg.get("result")

    def submit_worker(self, func: Callable[[], None]) -> None:
        """Run a UI-side worker for this account.

        Does **not** hold the IPC serial lock for the whole worker — only
        ``call()`` serializes helper RPC — so a long background folder sync
        cannot pin the lock before ``read_message`` is even sent (#437).
        """

        def runner() -> None:
            try:
                func()
            except Exception:
                log.debug(
                    "account worker failed account=%s",
                    self.account_uid,
                    exc_info=True,
                )

        threading.Thread(
            target=runner,
            name=f"camel-account-worker-{self.account_uid[:8]}",
            daemon=True,
        ).start()

    def kill(self) -> None:
        with self._lock:
            self._stop_unlocked(kill=True)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            if proc.poll() is None and proc.stdin is not None and self._alive:
                try:
                    write_message(proc.stdin, {"type": "shutdown", "id": "shutdown"})
                except Exception:
                    log.debug("shutdown write failed", exc_info=True)
            deadline = time.monotonic() + max(0.1, timeout)
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self._stop_unlocked(kill=True)

    def _reap_dead_unlocked(self) -> None:
        """Drop handles for an already-exited helper without SIGKILL (#445)."""
        proc = self._proc
        if proc is None:
            self._alive = False
            return
        if proc.poll() is None:
            # Still running — do not clear ``_alive`` here (would force a
            # mid-call kill on the next ensure_started).
            return
        self._alive = False
        try:
            proc.wait(timeout=0.1)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self._proc = None

    def _stop_unlocked(self, *, kill: bool) -> None:
        proc = self._proc
        self._alive = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if kill:
                    proc.kill()
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    proc.kill()
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            self._proc = None


@dataclass
class CamelRuntimePool:
    """Spawn / route / kill per-account Camel helpers."""

    _runtimes: dict[str, AccountCamelRuntime] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _on_account_died: Callable[[str], None] | None = field(
        default=None, init=False, repr=False
    )

    def set_account_died_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        self._on_account_died = callback

    def get(self, account_uid: str) -> AccountCamelRuntime:
        with self._lock:
            runtime = self._runtimes.get(account_uid)
            if runtime is None:
                runtime = AccountCamelRuntime(account_uid=account_uid)
                runtime.set_died_callback(self._handle_died)
                self._runtimes[account_uid] = runtime
            return runtime

    def _handle_died(self, account_uid: str) -> None:
        callback = self._on_account_died
        if callback is not None:
            try:
                callback(account_uid)
            except Exception:
                log.debug("account died callback failed", exc_info=True)

    def call(
        self,
        account_uid: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        timeout: float | None = _DEFAULT_JOB_TIMEOUT,
    ) -> Any:
        return self.get(account_uid).call(
            method, args, kwargs, timeout=timeout
        )

    def submit_worker(self, account_uid: str, func: Callable[[], None]) -> None:
        self.get(account_uid).submit_worker(func)

    def kill_account(self, account_uid: str) -> None:
        with self._lock:
            runtime = self._runtimes.pop(account_uid, None)
        if runtime is not None:
            runtime.kill()

    def shutdown_all(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.shutdown()
