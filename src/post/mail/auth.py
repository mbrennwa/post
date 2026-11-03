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

    try:
        listed = registry.list_sources()
    except TypeError:
        listed = ()
    if not listed:
        return sources
    try:
        candidates = list(listed)
    except TypeError:
        return sources

    for candidate in candidates:
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
        # Require a real string so unit-test MagicMocks do not call D-Bus (#478).
        if (
            isinstance(account_id, str)
            and account_id
            and account_id not in ids
        ):
            ids.append(account_id)
    return ids


def goa_account_ids(
    registry: EDataServer.SourceRegistry, source: EDataServer.Source
) -> list[str]:
    """GOA account ids related to this mail source (empty when not GOA)."""
    return _goa_account_ids(registry, source)


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


GoaEnsureOutcome = Literal["ok", "failed", "cancelled"]


def ensure_goa_credentials(
    registry: EDataServer.SourceRegistry,
    source: EDataServer.Source,
    cancellable: Gio.Cancellable | None = None,
) -> GoaEnsureOutcome:
    """Refresh GOA credentials so EDS can read them from the keyring.

    Returns ``ok`` when every GOA account responded successfully (or none apply),
    ``failed`` on a real EnsureCredentials / bus error, and ``cancelled`` when
    the call was preempted (#168). Callers must not treat ``cancelled`` as
    auth-dead (#478).
    """
    account_ids = _goa_account_ids(registry, source)
    if not account_ids:
        return "ok"
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, cancellable)
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            log.debug("GOA session bus get cancelled")
            return "cancelled"
        log.exception("Could not connect to session D-Bus for GOA")
        return "failed"

    outcome: GoaEnsureOutcome = "ok"
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
                return "cancelled"
            log.warning(
                "GOA EnsureCredentials failed for %s: %s",
                account_id,
                exc.message,
            )
            outcome = "failed"
    return outcome


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
            goa_outcome = ensure_goa_credentials(registry, source, cancellable)
            # Folder-list preempt cancels EnsureCredentials; do not fall through to a
            # password dialog for a cancelled background load (#168).
            if goa_outcome == "cancelled":
                _raise_if_cancelled(cancellable)
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(),
                    "Operation was cancelled",
                    Gio.IOErrorEnum.CANCELLED,
                )
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


def goa_attention_should_confirm(
    previous: bool | None, attention_needed: bool
) -> bool:
    """True when AttentionNeeded changed, or was already set at startup.

    A first observation of ``False`` is the baseline, not a sign-in event.
    """
    if previous is None:
        return attention_needed
    return previous != attention_needed


def read_goa_attention_needed(
    bus: Gio.DBusConnection, account_id: str
) -> bool | None:
    """Read ``AttentionNeeded`` for one GOA account. None when it cannot be read."""
    path = f"/org/gnome/OnlineAccounts/Accounts/{account_id}"
    try:
        result = bus.call_sync(
            _GOA_BUS,
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (_GOA_IFACE, "AttentionNeeded")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            5_000,
            None,
        )
    except GLib.Error:
        log.debug(
            "Could not read GOA AttentionNeeded for %s", account_id, exc_info=True
        )
        return None
    try:
        unpacked = result.unpack()
        value = unpacked[0] if isinstance(unpacked, tuple) else unpacked
        return bool(value)
    except (TypeError, IndexError, ValueError):
        log.debug("Unexpected AttentionNeeded payload for %s", account_id, exc_info=True)
        return None


class GoaAttentionMonitor:
    """Watch GOA ``AttentionNeeded`` from the UI process (#519).

    On a real transition, ``on_changed(account_id, attention_needed)`` runs.
    The caller confirms with one ``EnsureCredentials``; this monitor does not.
    """

    def __init__(self, on_changed: Callable[[str, bool], None]) -> None:
        self._on_changed = on_changed
        self._bus: Gio.DBusConnection | None = None
        self._subscription_id = 0
        self._last: dict[str, bool] = {}

    def start(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._subscription_id = self._bus.signal_subscribe(
            _GOA_BUS,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_properties_changed,
            None,
        )

    def note_account(self, account_id: str) -> None:
        """Record the current flag. Report only when attention is already needed."""
        bus = self._bus
        if bus is None or not account_id:
            return
        needed = read_goa_attention_needed(bus, account_id)
        if needed is None:
            return
        self._observe(account_id, needed)

    def _on_properties_changed(self, *_args: object) -> None:
        # connection, sender, path, interface, signal, params, user_data
        if len(_args) < 6:
            return
        path = _args[2]
        params = _args[5]
        if not isinstance(path, str) or not path.startswith(
            "/org/gnome/OnlineAccounts/Accounts/"
        ):
            return
        unpack = getattr(params, "unpack", None)
        if not callable(unpack):
            return
        try:
            interface, changed, invalidated = unpack()
        except (TypeError, ValueError):
            return
        if interface != _GOA_IFACE:
            return
        changed_map = changed if isinstance(changed, dict) else {}
        invalidated_names = invalidated if isinstance(invalidated, (list, tuple)) else ()
        if (
            "AttentionNeeded" not in changed_map
            and "AttentionNeeded" not in invalidated_names
        ):
            return
        account_id = path.rstrip("/").rsplit("/", 1)[-1]
        if not account_id:
            return
        if "AttentionNeeded" in changed_map:
            needed = bool(changed_map["AttentionNeeded"])
        else:
            bus = self._bus
            if bus is None:
                return
            read = read_goa_attention_needed(bus, account_id)
            if read is None:
                return
            needed = read
        self._observe(account_id, needed)

    def _observe(self, account_id: str, attention_needed: bool) -> None:
        previous = self._last.get(account_id)
        self._last[account_id] = attention_needed
        if not goa_attention_should_confirm(previous, attention_needed):
            return
        try:
            self._on_changed(account_id, attention_needed)
        except Exception:
            log.exception("GOA attention handler failed for %s", account_id)


def authentication_failed_error(message: str = "Authentication failed") -> GLib.Error:
    """GError Camel expects when Session.authenticate_sync fails."""
    return GLib.Error.new_literal(
        Camel.service_error_quark(),
        message,
        int(Camel.ServiceError.CANT_AUTHENTICATE),
    )
