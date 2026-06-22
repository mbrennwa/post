# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Outbound SMTP via smtplib (no Camel/GTK main-loop coupling)."""

from __future__ import annotations

import base64
import logging
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.utils import getaddresses

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("GLib", "2.0")

from gi.repository import EDataServer, GLib

from .auth import (
    PasswordPromptCallback,
    ensure_goa_credentials,
    lookup_stored_password,
)
from .send_errors import SendError, user_send_error_message

log = logging.getLogger(__name__)

_SECURITY_SSL = frozenset({"ssl", "ssl-on-alternate-port"})
_SECURITY_STARTTLS = frozenset({"starttls", "starttls-on-standard-port"})


@dataclass(frozen=True)
class SmtpTransportConfig:
    host: str
    port: int
    username: str
    security: str
    auth_method: str


def read_smtp_transport_config(
    registry: EDataServer.SourceRegistry, transport_uid: str
) -> tuple[EDataServer.Source, SmtpTransportConfig]:
    transport_source = registry.ref_source(transport_uid)
    if transport_source is None:
        raise ValueError(f"Unknown mail transport: {transport_uid}")

    auth = transport_source.get_extension("Authentication")
    host = (auth.get_host() or "").strip()
    if not host:
        raise ValueError("No SMTP host configured for this account")

    port = int(auth.get_port() or 25)
    username = (auth.get_user() or "").strip()
    auth_method = (auth.get_method() or "none").strip().lower()

    security = "none"
    if transport_source.has_extension("Security"):
        security = (
            transport_source.get_extension("Security").get_method() or "none"
        ).strip().lower()

    return transport_source, SmtpTransportConfig(
        host=host,
        port=port,
        username=username,
        security=security,
        auth_method=auth_method,
    )


def _recipient_addresses(
    to: list[str], cc: list[str] | None, bcc: list[str] | None
) -> list[str]:
    addresses: list[str] = []
    for header in (to, cc or [], bcc or []):
        for _name, addr in getaddresses(header):
            if addr:
                addresses.append(addr)
    return addresses


def _oauth2_auth_string(username: str, access_token: str) -> str:
    blob = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(blob.encode("utf-8")).decode("ascii")


def _lookup_password(
    registry: EDataServer.SourceRegistry,
    transport_source: EDataServer.Source,
    password_prompt: PasswordPromptCallback | None,
    *,
    auth_method: str,
) -> str | None:
    if auth_method in ("none", ""):
        return None
    ensure_goa_credentials(registry, transport_source, None)
    password = lookup_stored_password(registry, transport_source, None)
    if password:
        return password
    if password_prompt is None:
        return None
    return password_prompt(
        transport_source.get_display_name() or transport_source.get_uid(),
        auth_method.upper() if auth_method != "xoauth2" else "XOAUTH2",
    )


def _lookup_oauth2_token(
    transport_source: EDataServer.Source,
) -> str | None:
    try:
        ok, token, _expires_in = transport_source.get_oauth2_access_token_sync(None)
    except GLib.Error as exc:
        log.warning("OAuth2 token lookup failed: %s", exc.message)
        return None
    if ok and token:
        return token
    return None


def _connect_smtp(config: SmtpTransportConfig) -> smtplib.SMTP:
    timeout = 30
    if config.security in _SECURITY_SSL:
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=timeout,
            context=context,
        )
    smtp = smtplib.SMTP(config.host, config.port, timeout=timeout)
    smtp.ehlo()
    if config.security in _SECURITY_STARTTLS:
        context = ssl.create_default_context()
        smtp.starttls(context=context)
        smtp.ehlo()
    return smtp


def _authenticate_smtp(
    smtp: smtplib.SMTP,
    *,
    config: SmtpTransportConfig,
    registry: EDataServer.SourceRegistry,
    transport_source: EDataServer.Source,
    password_prompt: PasswordPromptCallback | None,
) -> None:
    method = config.auth_method
    if method in ("none", ""):
        return

    if method == "xoauth2":
        username = config.username
        if not username:
            raise SendError(user_send_error_message(RuntimeError("No SMTP user")))
        token = _lookup_oauth2_token(transport_source)
        if not token:
            raise SendError(user_send_error_message(RuntimeError("OAuth2 sign-in failed")))
        auth_string = _oauth2_auth_string(username, token)
        code, response = smtp.docmd("AUTH", "XOAUTH2 " + auth_string)
        if code != 235:
            raise SendError(
                user_send_error_message(
                    RuntimeError(f"SMTP auth failed: {response!r}")
                )
            )
        return

    username = config.username
    password = _lookup_password(
        registry,
        transport_source,
        password_prompt,
        auth_method=method,
    )
    if not username or not password:
        raise SendError(user_send_error_message(RuntimeError("SMTP credentials required")))
    smtp.login(username, password)


def send_via_smtp(
    *,
    registry: EDataServer.SourceRegistry,
    transport_uid: str,
    payload: bytes,
    envelope_from: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,
    password_prompt: PasswordPromptCallback | None = None,
) -> None:
    """Deliver a MIME message over SMTP without using Camel transport."""
    transport_source, config = read_smtp_transport_config(registry, transport_uid)
    recipients = _recipient_addresses(to, cc, bcc)
    if not recipients:
        raise ValueError("At least one recipient is required")

    smtp_start = time.monotonic()
    smtp: smtplib.SMTP | None = None
    try:
        smtp = _connect_smtp(config)
        _authenticate_smtp(
            smtp,
            config=config,
            registry=registry,
            transport_source=transport_source,
            password_prompt=password_prompt,
        )
        smtp.sendmail(envelope_from, recipients, payload)
    except SendError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise SendError(user_send_error_message(TimeoutError())) from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise SendError(user_send_error_message(exc)) from exc
    except smtplib.SMTPException as exc:
        raise SendError(user_send_error_message(exc)) from exc
    except OSError as exc:
        raise SendError(user_send_error_message(exc)) from exc
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                log.debug("SMTP quit failed", exc_info=True)

    log.debug(
        "SMTP (smtplib) send finished in %.2fs host=%s:%s",
        time.monotonic() - smtp_start,
        config.host,
        config.port,
    )
