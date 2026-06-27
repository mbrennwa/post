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
POST_LOCAL_TRANSPORT_UID = "post-local-sendmail"
BUILTIN_LOCAL_UID = "local"
EDS_LOCAL_DISPLAY_NAME = "Evolution Data Server"
LOCAL_BACKENDS = frozenset({"spool", "maildir"})
SYSTEM_SPOOL_DIRS = frozenset({"/var/mail", "/var/spool/mail"})

LocalMailType = Literal["spool", "maildir"]


@dataclass
class LocalMailConfig:
    enabled: bool
    mail_type: LocalMailType
    path: str
    from_name: str
    from_address: str


@dataclass
class MailAccount:
    uid: str
    name: str
    email: str | None
    backend: str | None
    identity_uid: str | None = None
    from_name: str | None = None
    from_address: str | None = None
    transport_uid: str | None = None

    @property
    def display_label(self) -> str:
        if self.uid == BUILTIN_LOCAL_UID:
            return EDS_LOCAL_DISPLAY_NAME
        return self.email or self.name

    @property
    def from_label(self) -> str:
        if self.from_name and self.from_address:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address or self.email or self.name

    @property
    def can_send(self) -> bool:
        return bool(self.transport_uid and (self.from_address or self.email))


def compose_from_accounts(
    sendable: list[MailAccount], preferred: MailAccount | None
) -> list[MailAccount]:
    """Return From accounts for compose, keeping the selected account first."""
    if preferred is None:
        return sendable
    if preferred.uid in {account.uid for account in sendable}:
        return sendable
    if preferred.from_address or preferred.email:
        return [preferred, *sendable]
    return sendable


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
    """True when EDS built-in local Maildir account has no messages."""
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


def _backend_settings_path(source: EDataServer.Source, ext_name: str) -> str:
    if not source.has_extension(ext_name):
        return ""
    ext = source.get_extension(ext_name)
    settings = ext.get_settings()
    path = settings.get_path() if hasattr(settings, "get_path") else None
    return str(path).strip() if path else ""


def get_local_backend_path(source: EDataServer.Source) -> str | None:
    """Return the configured spool/Maildir path for a local mail source."""
    mail_ext = source.get_extension("Mail Account")
    backend = mail_ext.get_backend_name()
    if backend not in LOCAL_BACKENDS:
        return None

    ext_name = "Spool Backend" if backend == "spool" else "Maildir Backend"
    path = _backend_settings_path(source, ext_name)
    return path or None


def get_mismatched_backend_path(source: EDataServer.Source) -> str:
    """Return a path stored under the wrong backend section, if any."""
    mail_ext = source.get_extension("Mail Account")
    backend = mail_ext.get_backend_name()
    if backend not in LOCAL_BACKENDS:
        return ""
    wrong_ext = "Maildir Backend" if backend == "spool" else "Spool Backend"
    return _backend_settings_path(source, wrong_ext)


def infer_spool_path_from_hint(hint: str) -> str:
    """Map a legacy or misconfigured path hint to a user mbox file."""
    hint = hint.strip()
    if not hint:
        return default_spool_path()
    user = os.environ.get("USER") or getpass.getuser()
    parent = os.path.dirname(hint)
    parent_real = os.path.realpath(parent) if parent else ""
    basename = os.path.basename(hint)
    if parent_real in SYSTEM_SPOOL_DIRS and basename != user:
        return default_spool_path()
    if os.path.isfile(hint):
        if os.access(hint, os.R_OK):
            return hint
        if parent_real in SYSTEM_SPOOL_DIRS:
            return default_spool_path()
        return hint
    if os.path.isdir(hint):
        user_file = os.path.join(hint, user)
        if os.path.isfile(user_file):
            return user_file
        parent_dir = os.path.dirname(user_file)
        if parent_dir and os.path.isdir(parent_dir):
            return user_file
    parent = os.path.dirname(hint)
    if parent and os.path.isdir(parent):
        return hint
    return default_spool_path()


def _is_local_backend_accessible(path: str, mail_type: LocalMailType) -> bool:
    path = path.strip()
    if not path:
        return False
    if mail_type == "maildir":
        return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
    if os.path.isfile(path):
        return os.access(path, os.R_OK)
    parent = os.path.dirname(path) or "/"
    if not os.path.isdir(parent):
        return False
    return os.access(parent, os.W_OK | os.X_OK)


def is_local_account_usable(source: EDataServer.Source) -> bool:
    """True when a spool/Maildir account has a readable storage path configured."""
    mail_ext = source.get_extension("Mail Account")
    backend = mail_ext.get_backend_name()
    if backend not in LOCAL_BACKENDS:
        return True

    path = get_local_backend_path(source)
    if not path or not os.path.isabs(path):
        return False
    return _is_local_backend_accessible(path, backend)  # type: ignore[arg-type]


def should_list_local_account(source: EDataServer.Source) -> bool:
    """Whether a spool/Maildir source should appear in Post's sidebar."""
    uid = source.get_uid()
    mail_ext = source.get_extension("Mail Account")
    backend = mail_ext.get_backend_name()
    if backend not in LOCAL_BACKENDS:
        return True
    if uid == BUILTIN_LOCAL_UID:
        return True
    if uid != POST_LOCAL_ACCOUNT_UID:
        log.debug("Skipping non-Post local account %s", uid)
        return False
    if not is_local_account_usable(source):
        log.debug("Skipping Post local account %s: no valid path", uid)
        return False
    return True


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
    path = _backend_settings_path(source, backend_ext)

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


def _transport_uid() -> str:
    return POST_LOCAL_TRANSPORT_UID


def _render_transport_source() -> str:
    return f"""[Data Source]
DisplayName=Local SMTP
Enabled=true
Parent={POST_LOCAL_ACCOUNT_UID}

[Mail Transport]
BackendName=smtp

[Authentication]
Host=127.0.0.1
Port=25
User=
Method=
RememberPassword=false
ProxyUid=system-proxy

[Security]
Method=none
"""


def _render_identity_source(from_name: str, from_address: str) -> str:
    name = from_name.replace("\n", " ").strip()
    address = from_address.replace("\n", " ").strip()
    return f"""[Data Source]
DisplayName={name or "Local mail"}
Enabled=true
Parent={POST_LOCAL_ACCOUNT_UID}

[Mail Identity]
Address={address}
Aliases=
Name={name}
Organization=
ReplyTo=
SignatureUid=none

[Mail Submission]
TransportUid={_transport_uid()}
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
    transport_path = os.path.join(sources_dir, f"{POST_LOCAL_TRANSPORT_UID}.source")
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
    _atomic_write(transport_path, _render_transport_source())


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


def _fresh_registry_with_local_sources(
    *, attempts: int = 20
) -> tuple[EDataServer.SourceRegistry, EDataServer.Source, EDataServer.Source]:
    last_registry: EDataServer.SourceRegistry | None = None
    for _ in range(attempts):
        last_registry = EDataServer.SourceRegistry.new_sync(None)
        if last_registry is None:
            time.sleep(0.05)
            continue
        source = last_registry.ref_source(POST_LOCAL_ACCOUNT_UID)
        identity = last_registry.ref_source(_identity_uid())
        transport = last_registry.ref_source(_transport_uid())
        if (
            source is not None
            and identity is not None
            and transport is not None
            and identity.has_extension("Mail Submission")
        ):
            return last_registry, source, identity
        time.sleep(0.05)
    raise RuntimeError("Local mail sources were not loaded after save")


def _local_identity_submission_uid(
    registry: EDataServer.SourceRegistry,
) -> str | None:
    identity = registry.ref_source(_identity_uid())
    if identity is None or not identity.has_extension("Mail Submission"):
        return None
    submission = identity.get_extension("Mail Submission")
    return submission.get_transport_uid() or None


def _sync_identity_submission(
    registry: EDataServer.SourceRegistry,
    identity: EDataServer.Source,
) -> None:
    if not identity.has_extension("Mail Submission"):
        raise RuntimeError("Local mail identity is missing a Mail Submission extension")
    submission = identity.get_extension("Mail Submission")
    submission.set_transport_uid(_transport_uid())
    registry.commit_source_sync(identity, None)


def _local_transport_is_configured(
    registry: EDataServer.SourceRegistry,
) -> bool:
    transport = registry.ref_source(_transport_uid())
    if transport is None or not transport.has_extension("Mail Transport"):
        return False
    backend = transport.get_extension("Mail Transport").get_backend_name()
    return backend == "smtp"


def repair_local_mail_config(registry: EDataServer.SourceRegistry) -> bool:
    """Rewrite a broken Post local mail source. Returns True when repaired."""
    source = registry.ref_source(POST_LOCAL_ACCOUNT_UID)
    if source is None or not source.get_enabled():
        return False
    if is_local_account_usable(source):
        return False

    config = read_local_mail_config(registry)
    if config is None:
        return False

    path = config.path.strip()
    if not path:
        hint = get_mismatched_backend_path(source)
        if config.mail_type == "spool":
            path = infer_spool_path_from_hint(hint)
        elif hint and os.path.isdir(hint):
            path = hint
        else:
            path = default_local_mail_config().path
    elif config.mail_type == "spool" and (
        os.path.isdir(path)
        or not _is_local_backend_accessible(path, "spool")
    ):
        path = infer_spool_path_from_hint(path)

    repaired = LocalMailConfig(
        enabled=config.enabled,
        mail_type=config.mail_type,
        path=path,
        from_name=config.from_name,
        from_address=config.from_address,
    )
    error = validate_local_mail_config(repaired)
    if error:
        log.warning("Cannot repair local mail config: %s", error)
        return False

    log.info("Repairing broken local mail source (path=%s)", path)
    apply_local_mail_config(repaired)
    return True


def ensure_post_local_mail_transport(
    registry: EDataServer.SourceRegistry,
) -> None:
    """Ensure enabled system mail has a local SMTP transport configured."""
    if repair_local_mail_config(registry):
        fresh = EDataServer.SourceRegistry.new_sync(None)
        if fresh is not None:
            registry = fresh

    config = read_local_mail_config(registry)
    if config is None or not config.enabled:
        return

    submission_uid = _local_identity_submission_uid(registry)
    if (
        not _local_transport_is_configured(registry)
        or submission_uid != _transport_uid()
    ):
        apply_local_mail_config(config)


def apply_local_mail_config(config: LocalMailConfig) -> None:
    """Create or update the Post-managed local mail account in EDS."""
    _write_local_sources(config)

    fresh, source, identity = _fresh_registry_with_local_sources()
    if identity is None:
        raise RuntimeError("Local mail identity was not loaded after save")

    ident_ext = identity.get_extension("Mail Identity")
    ident_ext.set_name(config.from_name.strip())
    ident_ext.set_address(config.from_address.strip())
    _sync_identity_submission(fresh, identity)

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
        return "Choose a mail file or folder."

    if config.mail_type == "maildir":
        if not os.path.isdir(path):
            return f"Mail folder does not exist: {path}"
        if not os.access(path, os.R_OK | os.X_OK):
            return f"Cannot read mail folder: {path}"
    elif os.path.isfile(path):
        if not os.access(path, os.R_OK):
            return f"Cannot read mail file: {path}"
    else:
        parent = os.path.dirname(path) or "/"
        if not os.path.isdir(parent):
            return f"Folder for the mail file does not exist: {parent}"

    if not config.from_address.strip():
        return "Enter a From address for local mail."

    return None
