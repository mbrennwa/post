# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""EDS credential lookup and IMAP password authentication."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Camel, EDataServer, Gio, GLib

if TYPE_CHECKING:
    from gi.repository import Gio as GioModule

log = logging.getLogger(__name__)

PasswordPromptCallback = Callable[[str, str | None], str | None]

_CREDENTIAL_PASSWORD = "password"

_GOA_BUS = "org.gnome.OnlineAccounts"
_GOA_IFACE = "org.gnome.OnlineAccounts.Account"
# Finite wait: unbounded EnsureCredentials blocks the shared mail I/O thread (#156).
_GOA_ENSURE_CREDENTIALS_TIMEOUT_MS = 15_000


def _related_credential_sources(
    registry: EDataServer.SourceRegistry, source: EDataServer.Source
) -> list[EDataServer.Source]:
    """Mail source plus any GOA collection source for the same account."""
    name = source.get_display_name()
    sources = [source]
    for candidate in registry.list_sources():
        if candidate.get_uid() == source.get_uid():
            continue
        if candidate.get_display_name() == name and candidate.has_extension(
            "GNOME Online Accounts"
        ):
            sources.append(candidate)
    return sources


def _goa_account_ids(
    registry: EDataServer.SourceRegistry, source: EDataServer.Source
) -> list[str]:
    ids: list[str] = []
    for candidate in _related_credential_sources(registry, source):
        if not candidate.has_extension("GNOME Online Accounts"):
            continue
        goa = candidate.get_extension("GNOME Online Accounts")
        account_id = goa.get_account_id()
        if account_id and account_id not in ids:
            ids.append(account_id)
    return ids


def ensure_goa_credentials(
    registry: EDataServer.SourceRegistry,
    source: EDataServer.Source,
    cancellable: Gio.Cancellable | None = None,
) -> None:
    """Refresh GOA credentials so EDS can read them from the keyring."""
    account_ids = _goa_account_ids(registry, source)
    if not account_ids:
        return
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, cancellable)
    except GLib.Error:
        log.exception("Could not connect to session D-Bus for GOA")
        return

    for account_id in account_ids:
        path = f"/org/gnome/OnlineAccounts/Accounts/{account_id}"
        try:
            bus.call_sync(
                _GOA_BUS,
                path,
                _GOA_IFACE,
                "EnsureCredentials",
                None,
                GLib.VariantType.new("(i)"),
                Gio.DBusCallFlags.NONE,
                _GOA_ENSURE_CREDENTIALS_TIMEOUT_MS,
                cancellable,
            )
        except GLib.Error as exc:
            log.warning("GOA EnsureCredentials failed for %s: %s", account_id, exc.message)


def lookup_stored_password(
    registry: EDataServer.SourceRegistry,
    source: EDataServer.Source,
    cancellable: Gio.Cancellable | None = None,
) -> str | None:
    """Look up a stored password via EDS (GOA, keyring, Evolution)."""
    provider = EDataServer.SourceCredentialsProvider.new(registry)
    for candidate in _related_credential_sources(registry, source):
        try:
            ok, creds = provider.lookup_sync(candidate, cancellable)
        except Exception:
            log.exception(
                "Credential lookup failed for %s", candidate.get_display_name()
            )
            continue
        if ok and creds is not None:
            password = creds.get(_CREDENTIAL_PASSWORD)
            if password:
                return password
    return None


def store_stored_password(
    registry: EDataServer.SourceRegistry,
    source: EDataServer.Source,
    password: str,
    cancellable: Gio.Cancellable | None = None,
) -> None:
    provider = EDataServer.SourceCredentialsProvider.new(registry)
    if not provider.can_store(source):
        return
    creds = EDataServer.NamedParameters.new()
    creds.set(_CREDENTIAL_PASSWORD, password)
    try:
        provider.store_sync(source, creds, True, cancellable)
    except GLib.Error:
        log.debug("Could not store password for %s", source.get_display_name())


def authenticate_service_sync(
    service: Camel.Service,
    source: EDataServer.Source,
    registry: EDataServer.SourceRegistry,
    mechanism: str | None,
    cancellable: Gio.Cancellable | None,
    password_prompt: PasswordPromptCallback | None,
) -> bool:
    """Authenticate an IMAP account using Camel's service-level SASL."""
    if mechanism == "XOAUTH2":
        return False

    for reprompt in (False, True):
        password: str | None = None
        if not reprompt:
            ensure_goa_credentials(registry, source, cancellable)
            password = lookup_stored_password(registry, source, cancellable)
            if not password:
                password = service.get_password()

        if not password and password_prompt is not None:
            password = password_prompt(
                source.get_display_name() or service.get_uid(), mechanism
            )

        if not password:
            return False

        service.set_password(password)
        result = service.authenticate_sync(mechanism, cancellable)
        if result == Camel.AuthenticationResult.ACCEPTED:
            store_stored_password(registry, source, password, cancellable)
            return True

        service.set_password("")
        if result == Camel.AuthenticationResult.REJECTED:
            log.warning(
                "Authentication rejected for %s (mechanism=%s)",
                source.get_display_name(),
                mechanism,
            )
            if password_prompt is not None:
                continue
            return False

        log.warning(
            "Authentication error for %s (mechanism=%s, result=%s)",
            source.get_display_name(),
            mechanism,
            int(result),
        )
        return False

    return False
