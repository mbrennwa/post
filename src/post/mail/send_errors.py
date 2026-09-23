# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""User-facing send error messages."""

from __future__ import annotations

import re
from collections.abc import Sequence

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

_MESSAGE_TOO_LARGE = "This message is too large to send."

_TOAST_SUBJECT_MAX = 48

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


def _token_expired_user_message(text: str) -> str | None:
    # Lazy import: network_errors imports _is_localhost_refused from this module.
    from post.mail.network_errors import (
        TOKEN_EXPIRED_FOLDER_MESSAGE,
        matches_token_expired_text,
    )

    if not matches_token_expired_text(text):
        return None
    return TOKEN_EXPIRED_FOLDER_MESSAGE


def _is_localhost_refused(text: str) -> bool:
    lowered = text.lower()
    if "connection refused" not in lowered:
        return False
    return bool(
        re.search(r"127\.0\.0\.1|localhost|::1", lowered)
        or "local host" in lowered
    )


def _is_message_too_large_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "errormessagesizeexceeded" in lowered
        or "message size exceeded" in lowered
        or "maximum supported size" in lowered
        or "too large to send" in lowered
    )


def format_outbound_item_label(
    subject: str | None,
    to: Sequence[str] | None = None,
) -> str:
    """Short label that identifies one Outbox item in a toast or dialog."""
    text = (subject or "").strip()
    if text:
        return f"“{_truncate_outbound_label(text)}”"
    if to:
        addr = (to[0] or "").strip()
        if addr:
            return f"The message to {_truncate_outbound_label(addr)}"
    return "“(no subject)”"


def name_outbound_in_reason(
    reason: str,
    subject: str | None = None,
    to: Sequence[str] | None = None,
) -> str:
    """Replace a vague 'this/the message' with the item's subject or recipient."""
    text = (reason or "").strip() or _GENERIC_SEND_FAILED
    label = format_outbound_item_label(subject, to)
    for prefix in ("This message ", "The message "):
        if text.startswith(prefix):
            return f"{label} {text[len(prefix):]}"
    return f"{label}: {text}"


def _outbox_failure_action(reason: str) -> str:
    """Actionable detail for an Outbox failure toast (why / what to do next)."""
    text = (reason or "").strip() or _GENERIC_SEND_FAILED
    if text == _GENERIC_SEND_FAILED:
        return "Check your account settings and try again."
    for prefix in (
        "The message could not be sent. ",
        "This message could not be sent. ",
        "This message ",
        "The message ",
    ):
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            if not rest:
                break
            if rest.startswith("is "):
                return f"It {rest}"
            if rest[0].islower():
                return rest[0].upper() + rest[1:]
            return rest
    return text


def format_outbox_failure_toast(
    reason: str,
    *,
    count: int = 1,
    subject: str | None = None,
    to: Sequence[str] | None = None,
) -> str:
    """Outbox send-failure toast: could not send, still in Outbox, then what to do.

    Order is fixed (#488): (1) named failure, (2) still in Outbox, (3) action/reason.
    """
    text = (reason or "").strip() or _GENERIC_SEND_FAILED
    if "could not be sent" in text.lower() and "still in Outbox" in text:
        return text

    action = _outbox_failure_action(text)
    if count > 1:
        lead = f"{count} messages could not be sent. They are still in Outbox."
    elif subject is not None or to:
        label = format_outbound_item_label(subject, to)
        lead = f"{label} could not be sent. The message is still in Outbox."
    else:
        lead = "Message could not be sent. The message is still in Outbox."
    return f"{lead} {action}"


def _truncate_outbound_label(text: str) -> str:
    if len(text) <= _TOAST_SUBJECT_MAX:
        return text
    return text[: _TOAST_SUBJECT_MAX - 1] + "…"


def user_send_error_message(exc: BaseException) -> str:
    """Return a short, user-friendly explanation for a send failure."""
    if isinstance(exc, SendQueued):
        return exc.user_message
    if isinstance(exc, SendError):
        token_message = _token_expired_user_message(exc.user_message)
        if token_message is not None:
            return token_message
        if _is_message_too_large_text(exc.user_message):
            return _MESSAGE_TOO_LARGE
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
            return "Add a recipient in the To field"
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

    token_message = _token_expired_user_message(text)
    if token_message is not None:
        return token_message

    if _is_message_too_large_text(text):
        return _MESSAGE_TOO_LARGE

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
        return "Add a recipient in the To field"

    if "to address" in lowered:
        return "Add a recipient in the To field"

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


def is_permanent_send_error(exc: BaseException) -> bool:
    """Return True when retrying this send will not succeed without a user change."""
    if isinstance(exc, SendQueued):
        return False
    if isinstance(exc, TimeoutError):
        return False
    from post.mail.network_errors import (
        is_queueable_network_error,
        is_sign_in_required_error,
    )

    if is_sign_in_required_error(exc):
        return False
    if is_queueable_network_error(exc):
        return False
    if is_compose_validation_error(exc):
        return True
    mapped = user_send_error_message(exc)
    raw = _raw_error_text(exc)
    combined = f"{mapped} {raw}".lower()
    if _is_message_too_large_text(combined):
        return True
    if any(
        token in combined
        for token in (
            "not set up for sending",
            "no from address",
            "system mail can only send",
            "too large to send",
        )
    ):
        return True
    return isinstance(exc, (SendError, ValueError))
