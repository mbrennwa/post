# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""User-facing send error messages."""

from __future__ import annotations

import re

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib


class SendError(Exception):
    """Send failed with a message suitable for display in the UI."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


SYSTEM_MAIL_EXTERNAL_RECIPIENTS = (
    "System mail can only send to addresses on this computer (user@localhost)."
)

_NO_LOCAL_MAIL_SERVER = (
    "No mail server is running on this computer. To send mail on the "
    "internet, choose a different From account or set up a local mail "
    "server (such as Postfix)."
)

_COULD_NOT_REACH_SERVER = (
    "Could not reach the mail server. Check your internet connection "
    "and account settings, then try again."
)

_SEND_TIMED_OUT = (
    "Sending took too long. Check your connection and try again."
)

_AUTH_FAILED = (
    "The mail server rejected your sign-in. Check your password or "
    "account settings, then try again."
)

_SECURE_CONNECTION_FAILED = (
    "Could not establish a secure connection to the mail server. "
    "Check your account security settings."
)

_GENERIC_SEND_FAILED = (
    "The message could not be sent. Check your account settings and try again."
)

MESSAGE_QUEUED = (
    "Message queued for sending when you're back online."
)


class SendQueued(SendError):
    """Send was deferred because the network is unavailable."""


def _raw_error_text(exc: BaseException) -> str:
    if isinstance(exc, SendError):
        return exc.user_message
    if isinstance(exc, GLib.Error):
        return exc.message or str(exc)
    message = str(exc).strip()
    if message.startswith("Could not send message:"):
        return message.removeprefix("Could not send message:").strip()
    return message


def _is_localhost_refused(text: str) -> bool:
    lowered = text.lower()
    if "connection refused" not in lowered:
        return False
    return bool(
        re.search(r"127\.0\.0\.1|localhost|::1", lowered)
        or "local host" in lowered
    )


def user_send_error_message(exc: BaseException) -> str:
    """Return a short, user-friendly explanation for a send failure."""
    if isinstance(exc, SendQueued):
        return exc.user_message
    if isinstance(exc, SendError):
        return exc.user_message

    if isinstance(exc, TimeoutError):
        return _SEND_TIMED_OUT

    if isinstance(exc, ValueError):
        text = str(exc).strip()
        lowered = text.lower()
        if "not valid" in lowered or "invalid address" in lowered:
            return text
        if (
            "at least one recipient" in lowered
            or "no recipients" in lowered
            or "to address" in lowered
        ):
            return "Add a recipient in the To field."
        if "line break" in lowered:
            return text
        if "linefeed" in lowered or "carriage return" in lowered:
            return "Recipient addresses must not contain line breaks."

    text = _raw_error_text(exc)
    lowered = text.lower()

    if _is_localhost_refused(text):
        return _NO_LOCAL_MAIL_SERVER

    if "connection refused" in lowered or "could not connect" in lowered:
        return _COULD_NOT_REACH_SERVER

    if "timed out" in lowered or "timeout" in lowered:
        return _SEND_TIMED_OUT

    if any(
        token in lowered
        for token in (
            "authentication",
            "auth failed",
            "invalid credentials",
            "login failed",
            "username and password",
        )
    ):
        return _AUTH_FAILED

    if any(
        token in lowered
        for token in ("certificate", "tls", "ssl", "handshake")
    ):
        return _SECURE_CONNECTION_FAILED

    if "no mail transport" in lowered:
        return "This account is not set up for sending mail."

    if "no from address" in lowered:
        return "This account has no From address configured."

    if "at least one recipient" in lowered:
        return "Add a recipient in the To field."

    if "to address" in lowered:
        return "Add a recipient in the To field."

    return _GENERIC_SEND_FAILED


def is_compose_validation_error(exc: BaseException) -> bool:
    """Return True when send failed due to invalid compose input, not the network."""
    if isinstance(exc, ValueError):
        text = str(exc).strip().lower()
    elif isinstance(exc, SendError):
        text = exc.user_message.lower()
    else:
        return False
    return (
        "line break" in text
        or "linefeed" in text
        or "carriage return" in text
        or "not valid" in text
        or "invalid address" in text
        or "no valid addresses" in text
    )
