# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""EDS credential lookup and IMAP/SMTP password authentication."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Camel, EDataServer, Gio, GLib

if TYPE_CHECKING:
    from gi.repository import Gio as GioModule

log = logging.getLogger(__name__)

PasswordPromptReason = Literal["check_mail", "send_mail"]
# (account_label, mechanism, reason, service_uid)
PasswordPromptCallback = Callable[
    [str, str | None, PasswordPromptReason | None, str | None],
    str | None,
]

_CREDENTIAL_PASSWORD = "password"

_GOA_BUS = "org.gnome.OnlineAccounts"
_GOA_IFACE = "org.gnome.OnlineAccounts.Account"
# Finite wait: unbounded EnsureCredentials blocks the shared mail I/O thread (#156).
_GOA_ENSURE_CREDENTIALS_TIMEOUT_MS = 15_000


def password_prompt_reason_for_source(
    source: EDataServer.Source,
) -> PasswordPromptReason | None:
    """Infer why Camel is asking for a password from the ESource type."""
    if source.has_extension("Mail Transport"):
        return "send_mail"
    if source.has_extension("Mail Account"):
        return "check_mail"
    return None


def _append_unique(
    sources: list[EDataServer.Source],
    seen: set[str],
    candidate: EDataServer.Source | None,
) -> None:
    if candidate is None:
        return
    uid = candidate.get_uid()
    if not uid or uid in seen:
        return
    seen.add(uid)
    sources.append(candidate)


def _related_credential_sources(
    registry: EDataServer.SourceRegistry, source: EDataServer.Source
) -> list[EDataServer.Source]:
    """Credential sources for an account: self, GOA collection, and siblings.

    SMTP transports and IMAP stores are separate ESources. Looking up only the
    transport UID misses a password already stored on the mail account or GOA
    collection (#168).
    """
    name = source.get_display_name()
    raw_parent = source.get_parent()
    parent_uid = raw_parent if isinstance(raw_parent, str) and raw_parent else None
    sources: list[EDataServer.Source] = []
    seen: set[str] = set()
    _append_unique(sources, seen, source)

    if parent_uid:
        _append_unique(sources, seen, registry.ref_source(parent_uid))

    for candidate in registry.list_sources():
        if candidate.get_uid() in seen:
            continue
        same_name = bool(name) and candidate.get_display_name() == name
        same_parent = bool(parent_uid) and candidate.get_parent() == parent_uid
        if not (same_name or same_parent):
            continue
        if candidate.has_extension("GNOME Online Accounts"):
            _append_unique(sources, seen, candidate)
            continue
        if candidate.has_extension("Mail Account"):
            _append_unique(sources, seen, candidate)
            continue
        if candidate.has_extension("Mail Transport"):
            _append_unique(sources, seen, candidate)
            continue
        if same_name and candidate.has_extension("Mail Identity"):
            # Identity itself has no password; its parent collection may.
            identity_parent = candidate.get_parent()
            if isinstance(identity_parent, str) and identity_parent:
                _append_unique(sources, seen, registry.ref_source(identity_parent))
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


def source_uses_goa(
    registry: EDataServer.SourceRegistry, source: EDataServer.Source
) -> bool:
    """Return True when this mail source is backed by GNOME Online Accounts."""
    return bool(_goa_account_ids(registry, source))


def open_gnome_online_accounts() -> bool:
    """Open GNOME Settings → Online Accounts. Return True if a launcher ran."""
    # Preferred: GNOME Settings deep-link (GNOME 42+).
    try:
        Gio.AppInfo.launch_default_for_uri("settings://online-accounts", None)
        return True
    except GLib.Error:
        log.debug("settings://online-accounts launch failed", exc_info=True)

    # Fallback: gnome-control-center panel.
    try:
        launcher = Gio.Subprocess.new(
            ["gnome-control-center", "online-accounts"],
            Gio.SubprocessFlags.NONE,
        )
        return launcher is not None
    except GLib.Error:
        log.warning("Could not open GNOME Online Accounts settings", exc_info=True)
        return False


def ensure_goa_credentials(
    registry: EDataServer.SourceRegistry,
    source: EDataServer.Source,
    cancellable: Gio.Cancellable | None = None,
) -> bool:
    """Refresh GOA credentials so EDS can read them from the keyring.

    Returns True when every GOA account responded successfully (or none apply).
    """
    account_ids = _goa_account_ids(registry, source)
    if not account_ids:
        return True
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, cancellable)
    except GLib.Error:
        log.exception("Could not connect to session D-Bus for GOA")
        return False

    ok = True
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
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                log.debug(
                    "GOA EnsureCredentials cancelled for %s",
                    account_id,
                )
                ok = False
            else:
                log.warning(
                    "GOA EnsureCredentials failed for %s: %s",
                    account_id,
                    exc.message,
                )
                ok = False
    return ok


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
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                log.debug(
                    "Credential lookup cancelled for %s",
                    candidate.get_display_name(),
                )
            else:
                log.warning(
                    "Credential lookup failed for %s: %s",
                    candidate.get_display_name(),
                    exc.message,
                )
            continue
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


def _raise_if_cancelled(cancellable: Gio.Cancellable | None) -> None:
    if cancellable is not None and cancellable.is_cancelled():
        raise GLib.Error.new_literal(
            Gio.io_error_quark(),
            "Operation was cancelled",
            Gio.IOErrorEnum.CANCELLED,
        )


def authenticate_service_sync(
    service: Camel.Service,
    source: EDataServer.Source,
    registry: EDataServer.SourceRegistry,
    mechanism: str | None,
    cancellable: Gio.Cancellable | None,
    password_prompt: PasswordPromptCallback | None,
) -> bool:
    """Authenticate a Camel store/transport using Camel's service-level SASL."""
    if mechanism == "XOAUTH2":
        return False

    reason = password_prompt_reason_for_source(source)
    service_uid = service.get_uid()

    for reprompt in (False, True):
        _raise_if_cancelled(cancellable)
        password: str | None = None
        if not reprompt:
            ensure_goa_credentials(registry, source, cancellable)
            # Folder-list preempt cancels EnsureCredentials; do not fall through to a
            # password dialog for a cancelled background load (#168).
            _raise_if_cancelled(cancellable)
            password = lookup_stored_password(registry, source, cancellable)
            _raise_if_cancelled(cancellable)
            if not password:
                password = service.get_password()

        if not password and password_prompt is not None:
            password = password_prompt(
                source.get_display_name() or service_uid,
                mechanism,
                reason,
                service_uid,
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


def authentication_failed_error(message: str = "Authentication failed") -> GLib.Error:
    """GError Camel expects when Session.authenticate_sync fails."""
    return GLib.Error.new_literal(
        Camel.service_error_quark(),
        message,
        int(Camel.ServiceError.CANT_AUTHENTICATE),
    )
