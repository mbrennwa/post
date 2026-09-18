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
import sys
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


def _build_mail_service():
    from post.mail.eds import MailService
    from post.mail.io_thread import get_mail_io_thread

    # Ensure Camel.init / mail thread exist in this process before connect.
    get_mail_io_thread()
    return MailService.connect()


def _dispatch(mail: Any, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    if method == "ping":
        return {"ok": True, "pid": os.getpid(), "account_uid": args[0] if args else None}
    if method == "sleep_for_test":
        import time

        seconds = float(args[0]) if args else 0.0
        time.sleep(max(0.0, seconds))
        return {"slept": seconds}
    if method not in ALLOWED_METHODS:
        raise PermissionError(f"method not allowed in camel helper: {method}")
    func = getattr(mail, method, None)
    if func is None or not callable(func):
        raise AttributeError(f"MailService has no method {method!r}")
    return func(*args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Post per-account Camel helper")
    parser.add_argument("--account-uid", required=True)
    args = parser.parse_args(argv)
    account_uid = args.account_uid

    # Mark this process as a helper so nested code can avoid spawning helpers.
    os.environ["POST_MAIL_CAMEL_HELPER_PROCESS"] = "1"
    os.environ["POST_MAIL_CAMEL_HELPERS"] = "0"

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
        mail = _build_mail_service()
    except Exception:
        log.exception("failed to connect MailService in helper")
        return 1

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    write_message(
        stdout,
        {"type": "ready", "account_uid": account_uid, "pid": os.getpid()},
    )

    while True:
        try:
            msg = read_message(stdin)
        except Exception:
            log.exception("IPC read failed")
            return 1
        if msg is None:
            log.info("helper stdin closed account=%s", account_uid)
            return 0
        msg_type = msg.get("type")
        if msg_type == "shutdown":
            write_message(stdout, {"type": "bye", "id": msg.get("id")})
            return 0
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
        try:
            result = _dispatch(mail, method, call_args, call_kwargs)
            write_message(
                stdout,
                {"type": "result", "id": req_id, "ok": True, "result": result},
            )
        except BaseException as exc:
            log.debug(
                "helper call failed method=%s account=%s",
                method,
                account_uid,
                exc_info=True,
            )
            write_message(
                stdout,
                {
                    "type": "result",
                    "id": req_id,
                    "ok": False,
                    "error": str(exc) or repr(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )


if __name__ == "__main__":
    raise SystemExit(main())
