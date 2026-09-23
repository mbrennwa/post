# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Per-account Camel helper process (#437 / #422 Phase 3).

One OS process owns one account's ``MailSession`` and a private ``post-mail-io``
thread. The UI process talks JSON-IPC (stdin/stdout) and can kill this process
without taking down other accounts.

Run::

    python3 -m post.mail.camel_helper --account-uid <uid>
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import traceback
from typing import Any

log = logging.getLogger(__name__)

# Methods the UI may invoke on the helper's MailService (JSON-serializable I/O).
ALLOWED_METHODS = frozenset(
    {
        "ping",
        "sleep_for_test",
        "get_oauth2_access_token_for_account",
        "list_folders",
        "get_folder_stats",
        "get_account_folder_stats",
        "list_messages",
        "list_messages_page",
        "get_folder_messages",
        "read_message",
        "read_attachment_data",
        "save_draft",
        "delete_draft",
        "move_messages",
        "archive_messages",
        "move_messages_to_trash",
        "apply_local_mutation",
        "flush_account_operation_queue",
        "toggle_message_seen",
        "toggle_message_flagged",
        "toggle_messages_seen",
        "toggle_messages_flagged",
        "set_messages_seen",
        "set_messages_flagged",
        "mark_message_read",
        "flush_send_queue",
        "deliver_outbound_queue_item",
        "continue_heavy_folder_index",
        "search_folder_messages",
        "get_correspondents",
        "invalidate_account_connection",
        "set_network_available",
        "go_online_sync",
        "list_offline_downsync_folders",
        "offline_downsync_folder",
        "synchronize_folder_message",
    }
)


def _configure_logging() -> None:
    level_name = (os.environ.get("POST_LOG_LEVEL") or "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(levelname)s camel-helper %(message)s",
        stream=sys.stderr,
    )


def _build_mail_service(account_uid: str):
    from post.mail.eds import MailService
    from post.mail.io_thread import get_mail_io_thread

    # Ensure Camel.init / mail thread exist in this process before connect.
    get_mail_io_thread()
    return MailService.connect(account_uid=account_uid)


def _sleep_for_test(mail: Any, seconds: float) -> dict[str, Any]:
    """Interruptible sleep so ``cancel`` can free helper ``_serial`` in tests (#482)."""
    import time

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    cancellable = Gio.Cancellable()
    register = getattr(mail, "_register_helper_op_cancellable", None)
    clear = getattr(mail, "_clear_helper_op_cancellable", None)
    if callable(register):
        register(cancellable)
    try:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if cancellable.is_cancelled():
                return {"slept": seconds, "cancelled": True}
            time.sleep(0.05)
        return {"slept": seconds}
    finally:
        if callable(clear):
            clear(cancellable)


def _dispatch(mail: Any, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    if method == "ping":
        return {"ok": True, "pid": os.getpid(), "account_uid": args[0] if args else None}
    if method == "sleep_for_test":
        seconds = float(args[0]) if args else 0.0
        return _sleep_for_test(mail, seconds)
    if method not in ALLOWED_METHODS:
        raise PermissionError(f"method not allowed in camel helper: {method}")
    func = getattr(mail, method, None)
    if func is None or not callable(func):
        raise AttributeError(f"MailService has no method {method!r}")
    return func(*args, **kwargs)


def _result_payload(req_id: Any, result: Any) -> dict[str, Any]:
    return {"type": "result", "id": req_id, "ok": True, "result": result}


def _error_payload(req_id: Any, exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "result",
        "id": req_id,
        "ok": False,
        "error": str(exc) or repr(exc),
        "error_type": type(exc).__name__,
        "traceback": traceback.format_exc(),
    }
    # Preserve MessageNotAvailableError.reason across IPC — without it
    # the UI treats every miss as VANISHED and removes the list row
    # (wrong for GOA/sign-in cache misses on M365).
    try:
        from post.mail.eds import MessageNotAvailableError

        if isinstance(exc, MessageNotAvailableError):
            payload["error_details"] = {
                "message_uid": exc.message_uid,
                "folder_name": exc.folder_name,
                "reason": exc.reason,
            }
    except Exception:
        pass
    return payload


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Post per-account Camel helper")
    parser.add_argument("--account-uid", required=True)
    args = parser.parse_args(argv)
    account_uid = args.account_uid

    # Mark this process as a helper so nested code does not spawn helpers.
    os.environ["POST_MAIL_CAMEL_HELPER_PROCESS"] = "1"

    from post.mail.camel_paths import configure_helper_camel_dirs

    # Private Camel dirs — do not share ~/.local/share/evolution across helpers (#445).
    data_dir, cache_dir = configure_helper_camel_dirs(account_uid)
    log.info(
        "camel helper dirs account=%s data=%s cache=%s",
        account_uid,
        data_dir,
        cache_dir,
    )

    from post.mail.camel_ipc import read_message, write_message

    log.info("starting camel helper account=%s pid=%s", account_uid, os.getpid())
    try:
        mail = _build_mail_service(account_uid)
    except Exception:
        log.exception("failed to connect MailService in helper")
        return 1

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    write_message(
        stdout,
        {"type": "ready", "account_uid": account_uid, "pid": os.getpid()},
    )

    # Stdin reader stays alive while a call runs so ``cancel`` can preempt
    # offline downsync without waiting for the call to finish (#482).
    incoming: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def _stdin_reader() -> None:
        try:
            while True:
                try:
                    msg = read_message(stdin)
                except Exception:
                    log.exception("IPC read failed")
                    incoming.put(None)
                    return
                if msg is None:
                    incoming.put(None)
                    return
                incoming.put(msg)
        except Exception:
            log.exception("stdin reader crashed")
            incoming.put(None)

    threading.Thread(
        target=_stdin_reader,
        name="camel-helper-stdin",
        daemon=True,
    ).start()

    while True:
        msg = incoming.get()
        if msg is None:
            log.info("helper stdin closed account=%s", account_uid)
            return 0
        msg_type = msg.get("type")
        if msg_type == "shutdown":
            cancel_op = getattr(mail, "cancel_helper_op", None)
            if callable(cancel_op):
                cancel_op()
            write_message(stdout, {"type": "bye", "id": msg.get("id")})
            return 0
        if msg_type == "cancel":
            cancel_op = getattr(mail, "cancel_helper_op", None)
            if callable(cancel_op):
                cancel_op()
            continue
        if msg_type != "call":
            write_message(
                stdout,
                {
                    "type": "result",
                    "id": msg.get("id"),
                    "ok": False,
                    "error": f"unknown message type {msg_type!r}",
                    "error_type": "ValueError",
                },
            )
            continue
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        call_args = msg.get("args") or []
        call_kwargs = msg.get("kwargs") or {}
        if not isinstance(call_args, list):
            call_args = []
        if not isinstance(call_kwargs, dict):
            call_kwargs = {}

        done = threading.Event()
        box: dict[str, Any] = {}

        def _run_call(
            mid: str = method,
            cargs: list[Any] = call_args,
            ckwargs: dict[str, Any] = call_kwargs,
        ) -> None:
            try:
                box["result"] = _dispatch(mail, mid, cargs, ckwargs)
                box["ok"] = True
            except BaseException as exc:
                box["exc"] = exc
                box["ok"] = False
                log.debug(
                    "helper call failed method=%s account=%s",
                    mid,
                    account_uid,
                    exc_info=True,
                )
            finally:
                done.set()

        worker = threading.Thread(
            target=_run_call,
            name=f"camel-helper-call-{method[:24]}",
            daemon=True,
        )
        worker.start()

        # Drain cancel/shutdown while the call runs; UI still serializes calls.
        shutdown_requested = False
        while not done.wait(timeout=0.05):
            try:
                extra = incoming.get_nowait()
            except queue.Empty:
                continue
            if extra is None:
                cancel_op = getattr(mail, "cancel_helper_op", None)
                if callable(cancel_op):
                    cancel_op()
                done.wait(timeout=30.0)
                return 0
            extra_type = extra.get("type")
            if extra_type == "cancel":
                cancel_op = getattr(mail, "cancel_helper_op", None)
                if callable(cancel_op):
                    cancel_op()
            elif extra_type == "shutdown":
                cancel_op = getattr(mail, "cancel_helper_op", None)
                if callable(cancel_op):
                    cancel_op()
                shutdown_requested = True
                done.wait(timeout=30.0)
                write_message(stdout, {"type": "bye", "id": extra.get("id")})
                return 0
            elif extra_type == "call":
                # Should not happen while UI holds _serial; re-queue after.
                incoming.put(extra)

        worker.join(timeout=1.0)
        if shutdown_requested:
            return 0
        if box.get("ok"):
            write_message(stdout, _result_payload(req_id, box.get("result")))
        else:
            write_message(
                stdout,
                _error_payload(req_id, box.get("exc") or RuntimeError("helper call failed")),
            )


if __name__ == "__main__":
    raise SystemExit(main())
