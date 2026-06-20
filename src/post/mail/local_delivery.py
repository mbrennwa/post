# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deliver outbound mail directly to a local spool or Maildir."""

from __future__ import annotations

import fcntl
import os
import socket
import time
from email.utils import formatdate
from typing import Any

import gi

gi.require_version("Camel", "1.2")
gi.require_version("Gio", "2.0")

from gi.repository import Gio

from .accounts import LocalMailConfig, LocalMailType
from .compose import normalize_email


def mime_message_to_bytes(message: Any) -> bytes:
    stream = Gio.MemoryOutputStream.new_resizable()
    message.write_to_output_stream_sync(stream, None)
    stream.close(None)
    return bytes(stream.steal_as_bytes().get_data())


def _mbox_escape_content(content: bytes) -> bytes:
    lines = content.split(b"\n")
    escaped: list[bytes] = []
    for line in lines:
        if line.startswith(b"From "):
            escaped.append(b">" + line)
        else:
            escaped.append(line)
    return b"\n".join(escaped)


def _mbox_from_line(envelope_from: str) -> bytes:
    address = normalize_email(envelope_from) or "MAILER-DAEMON"
    return f"From {address} {formatdate(localtime=True)}\n".encode("ascii")


def deliver_to_spool(spool_path: str, raw_message: bytes, *, envelope_from: str) -> None:
    payload = _mbox_escape_content(raw_message.rstrip(b"\n")) + b"\n"
    with open(spool_path, "ab") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(_mbox_from_line(envelope_from))
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def deliver_to_maildir(maildir_path: str, raw_message: bytes) -> None:
    hostname = socket.gethostname().split(".")[0] or "localhost"
    unique = f"{int(time.time() * 1_000_000)}.{os.getpid()}.{hostname}"
    tmp_dir = os.path.join(maildir_path, "tmp")
    new_dir = os.path.join(maildir_path, "new")
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(new_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, unique)
    new_path = os.path.join(new_dir, unique)
    with open(tmp_path, "wb") as handle:
        handle.write(raw_message)
        if not raw_message.endswith(b"\n"):
            handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(tmp_path, new_path)


def deliver_local_message(
    config: LocalMailConfig,
    message: Any,
    *,
    envelope_from: str,
) -> None:
    raw_message = mime_message_to_bytes(message)
    if config.mail_type == "spool":
        deliver_to_spool(config.path, raw_message, envelope_from=envelope_from)
        return
    deliver_to_maildir(config.path, raw_message)


def _local_domains(local_address: str) -> set[str]:
    domains = {"localhost", "localhost.localdomain"}
    hostname = socket.gethostname().strip().lower()
    if hostname:
        domains.add(hostname)
        domains.add(hostname.split(".")[0])
    _, _, domain = normalize_email(local_address).partition("@")
    if domain:
        domains.add(domain.lower())
    return domains


def is_local_recipient(address: str, *, local_address: str) -> bool:
    normalized = normalize_email(address)
    if not normalized:
        return False
    if "@" not in normalized:
        return True
    _user, domain = normalized.rsplit("@", 1)
    return domain.lower() in _local_domains(local_address)


def all_recipients_local(
    *,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    local_address: str,
) -> bool:
    recipients = [*to, *(cc or []), *(bcc or [])]
    if not recipients:
        return False
    return all(
        is_local_recipient(address, local_address=local_address)
        for address in recipients
    )


def can_deliver_locally(
    config: LocalMailConfig,
    *,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
) -> bool:
    if not config.enabled:
        return False
    path = config.path.strip()
    if not path:
        return False
    if config.mail_type == "spool" and not os.path.isfile(path):
        parent = os.path.dirname(path)
        if not parent or not os.path.isdir(parent):
            return False
    if config.mail_type == "maildir" and not os.path.isdir(path):
        return False
    return all_recipients_local(
        to=to,
        cc=cc,
        bcc=bcc,
        local_address=config.from_address,
    )
