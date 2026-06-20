# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local (non-online) mail account setup via Evolution Data Server."""

from __future__ import annotations

import getpass
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Literal

import gi

gi.require_version("EDataServer", "1.2")

from gi.repository import EDataServer

log = logging.getLogger(__name__)

POST_LOCAL_ACCOUNT_UID = "post-local-mail"
BUILTIN_LOCAL_UID = "local"

LocalMailType = Literal["spool", "maildir"]


@dataclass
class LocalMailConfig:
    enabled: bool
    mail_type: LocalMailType
    path: str
    from_name: str
    from_address: str


def default_spool_path() -> str:
    user = os.environ.get("USER") or getpass.getuser()
    return f"/var/spool/mail/{user}"


def default_local_mail_config() -> LocalMailConfig:
    user = os.environ.get("USER") or getpass.getuser()
    return LocalMailConfig(
        enabled=False,
        mail_type="spool",
        path=default_spool_path(),
        from_name=user,
        from_address=f"{user}@localhost",
    )


def evolution_sources_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "evolution", "sources")


def _identity_uid() -> str:
    return f"{POST_LOCAL_ACCOUNT_UID}-identity"


def is_maildir_empty(path: str) -> bool:
    """Return True when a Maildir tree has no messages in cur/ or new/."""
    if not os.path.isdir(path):
        return True
    for subdir_name in ("cur", "new"):
        subdir = os.path.join(path, subdir_name)
        if not os.path.isdir(subdir):
            continue
        for name in os.listdir(subdir):
            if name.startswith("."):
                continue
            return False
    return True


def is_spool_empty(path: str) -> bool:
    """Return True when an mbox spool file is missing or has no content."""
    if not os.path.isfile(path):
        return True
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return True


def is_builtin_local_store_empty(registry: EDataServer.SourceRegistry) -> bool:
    """True when EDS built-in 'On This Computer' maildir has no messages."""
    try:
        source = registry.ref_builtin_mail_account()
    except Exception:
        log.exception("Could not read built-in local mail account")
        return True

    if source is None or source.get_uid() != BUILTIN_LOCAL_UID:
        return True

    if not source.has_extension("Maildir Backend"):
        return True

    maildir = source.get_extension("Maildir Backend")
    path = maildir.get_property("path")
    if not path:
        settings = maildir.get_settings()
        if hasattr(settings, "get_path"):
            path = settings.get_path()
    if not path:
        return True
    return is_maildir_empty(path)


def read_local_mail_config(
    registry: EDataServer.SourceRegistry,
) -> LocalMailConfig | None:
    source = registry.ref_source(POST_LOCAL_ACCOUNT_UID)
    if source is None:
        return None

    mail_ext = source.get_extension("Mail Account")
    backend = mail_ext.get_backend_name()
    if backend not in ("spool", "maildir"):
        return None

    path = ""
    backend_ext = (
        "Spool Backend" if backend == "spool" else "Maildir Backend"
    )
    if source.has_extension(backend_ext):
        ext = source.get_extension(backend_ext)
        settings = ext.get_settings()
        path = settings.get_path() or ""

    from_name = ""
    from_address = ""
    identity_uid = mail_ext.get_identity_uid()
    if identity_uid:
        identity = registry.ref_source(identity_uid)
        if identity and identity.has_extension("Mail Identity"):
            ident = identity.get_extension("Mail Identity")
            from_name = ident.get_name() or ""
            from_address = ident.get_address() or ""

    return LocalMailConfig(
        enabled=source.get_enabled(),
        mail_type=backend,  # type: ignore[arg-type]
        path=path,
        from_name=from_name,
        from_address=from_address,
    )


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _render_identity_source(from_name: str, from_address: str) -> str:
    name = from_name.replace("\n", " ").strip()
    address = from_address.replace("\n", " ").strip()
    return f"""[Data Source]
DisplayName={name or "Local mail"}
Enabled=true
Parent=

[Mail Identity]
Address={address}
Aliases=
Name={name}
Organization=
ReplyTo=
SignatureUid=none
"""


def _render_account_source(
    *,
    enabled: bool,
    mail_type: LocalMailType,
    path: str,
) -> str:
    backend_section = "Spool Backend" if mail_type == "spool" else "Maildir Backend"
    return f"""[Data Source]
DisplayName=Local mail
Enabled={'true' if enabled else 'false'}
Parent=

[Mail Account]
BackendName={mail_type}
IdentityUid={_identity_uid()}
ArchiveFolder=
NeedsInitialSetup=false
MarkSeen=inconsistent
MarkSeenTimeout=1500
Builtin=false

[{backend_section}]
Path={path}
FilterInbox=true
StoreChangesInterval=3
FilterAll=false
FilterJunk=true

[Refresh]
Enabled=false
"""


def _write_local_sources(config: LocalMailConfig) -> None:
    sources_dir = evolution_sources_dir()
    identity_path = os.path.join(sources_dir, f"{_identity_uid()}.source")
    account_path = os.path.join(sources_dir, f"{POST_LOCAL_ACCOUNT_UID}.source")
    _atomic_write(
        identity_path,
        _render_identity_source(config.from_name, config.from_address),
    )
    _atomic_write(
        account_path,
        _render_account_source(
            enabled=config.enabled,
            mail_type=config.mail_type,
            path=config.path,
        ),
    )


def _sync_backend_path(
    registry: EDataServer.SourceRegistry,
    config: LocalMailConfig,
) -> None:
    source = registry.ref_source(POST_LOCAL_ACCOUNT_UID)
    if source is None:
        return

    backend_ext = (
        "Spool Backend" if config.mail_type == "spool" else "Maildir Backend"
    )
    if not source.has_extension(backend_ext):
        return

    ext = source.get_extension(backend_ext)
    settings = ext.get_settings()
    if settings.get_path() != config.path:
        settings.set_path(config.path)
        registry.commit_source_sync(source, None)


def _fresh_registry_with_source(
    uid: str, *, attempts: int = 10
) -> tuple[EDataServer.SourceRegistry, EDataServer.Source]:
    last_registry: EDataServer.SourceRegistry | None = None
    for _ in range(attempts):
        last_registry = EDataServer.SourceRegistry.new_sync(None)
        if last_registry is None:
            time.sleep(0.05)
            continue
        source = last_registry.ref_source(uid)
        if source is not None:
            return last_registry, source
        time.sleep(0.05)
    raise RuntimeError(f"Mail source “{uid}” was not loaded after save")


def apply_local_mail_config(config: LocalMailConfig) -> None:
    """Create or update the Post-managed local mail account in EDS."""
    _write_local_sources(config)

    fresh, source = _fresh_registry_with_source(POST_LOCAL_ACCOUNT_UID)
    identity = fresh.ref_source(_identity_uid())
    if identity is None:
        raise RuntimeError("Local mail identity was not loaded after save")

    ident_ext = identity.get_extension("Mail Identity")
    ident_ext.set_name(config.from_name.strip())
    ident_ext.set_address(config.from_address.strip())
    fresh.commit_source_sync(identity, None)

    source.set_display_name("Local mail")
    source.set_enabled(config.enabled)
    mail_ext = source.get_extension("Mail Account")
    mail_ext.set_backend_name(config.mail_type)
    mail_ext.set_identity_uid(_identity_uid())
    mail_ext.set_needs_initial_setup(False)
    fresh.commit_source_sync(source, None)

    _sync_backend_path(fresh, config)


def validate_local_mail_config(config: LocalMailConfig) -> str | None:
    """Return an error message when config is invalid, else None."""
    if not config.enabled:
        return None

    path = config.path.strip()
    if not path:
        return "Choose a mail spool file or Maildir folder."

    if config.mail_type == "maildir":
        if not os.path.isdir(path):
            return f"Maildir folder does not exist: {path}"
    elif not os.path.isfile(path):
        parent = os.path.dirname(path) or "/"
        if not os.path.isdir(parent):
            return f"Spool directory does not exist: {parent}"

    if not config.from_address.strip():
        return "Enter a From address for local mail."

    return None
