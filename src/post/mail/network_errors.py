# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Classify network/auth mail failures and format folder-load errors."""

from __future__ import annotations

import logging

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from .send_errors import _is_localhost_refused

OFFLINE_FOLDER_MESSAGE = "Offline — folders unavailable until you reconnect."
SIGN_IN_FOLDER_MESSAGE = (
    "Sign-in required — reconnect this account to load folders."
)
TOKEN_EXPIRED_FOLDER_MESSAGE = (
    "Sign-in expired — open Settings → Online Accounts to reconnect."
)


def is_queueable_network_error(exc: BaseException) -> bool:
    """Return True when a send failure may succeed if retried later."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, GLib.Error):
        text = exc.message or str(exc)
        if _is_localhost_refused(text):
            return False
        lowered = text.lower()
        return any(
            token in lowered
            for token in (
                "could not connect",
                "connection refused",
                "network is unreachable",
                "no route to host",
                "name or service not known",
                "temporary failure",
                "timed out",
                "timeout",
                "i/o error",
                "connection reset",
                "broken pipe",
                "network error",
            )
        )
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, GLib.Error):
        return exc.message or str(exc)
    return str(exc)


def is_network_unavailable_error(exc: BaseException) -> bool:
    """Return True for DNS/network failures and Camel offline-service errors."""
    if is_queueable_network_error(exc):
        return True
    lowered = _error_text(exc).lower()
    return any(
        token in lowered
        for token in (
            "must be working online",
            "error resolving",
            "name resolution",
        )
    )


def is_sign_in_required_error(exc: BaseException) -> bool:
    """Return True when the account needs interactive re-authentication."""
    lowered = _error_text(exc).lower()
    return any(
        token in lowered
        for token in (
            "access token",
            "refresh token",
            "aadsts",
            "goa-error",
            "authentication",
            "auth failed",
            "invalid credentials",
            "login failed",
            "password",
            "sign-in",
            "sign in",
            "oauth",
        )
    )


def format_folder_load_error(exc: BaseException) -> str:
    """User-facing folder-list failure text (never raw GLib/Camel dumps)."""
    if is_network_unavailable_error(exc):
        return OFFLINE_FOLDER_MESSAGE
    lowered = _error_text(exc).lower()
    if any(
        token in lowered
        for token in ("access token", "refresh token", "aadsts", "goa-error", "oauth")
    ):
        return TOKEN_EXPIRED_FOLDER_MESSAGE
    if is_sign_in_required_error(exc):
        return SIGN_IN_FOLDER_MESSAGE
    return "Could not load folders for this account."


def format_sign_in_required_log(exc: BaseException) -> str:
    """Short log detail for expected auth failures (no AADSTS / GOA dumps)."""
    text = _error_text(exc)
    lowered = text.lower()
    if any(
        token in lowered
        for token in ("access token", "refresh token", "aadsts", "goa-error", "oauth")
    ):
        return "sign-in expired or invalid (re-auth required)"
    if "password" in lowered or "credentials" in lowered:
        return "password or credentials required"
    return "sign-in required"


def log_mail_error(logger: logging.Logger, message: str, exc: BaseException) -> None:
    if is_network_unavailable_error(exc):
        logger.debug("%s: %s", message, exc)
    elif is_sign_in_required_error(exc):
        # Expected until the user re-auths (expired GOA/OAuth token, etc.).
        logger.warning("%s: %s", message, format_sign_in_required_log(exc))
    else:
        logger.exception(message)
