# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later
#
# EDS/Camel glue derived from EvolutionMCP (MIT) — see LICENSES/MIT-EvolutionMCP.txt

"""EDS SourceRegistry + Camel session."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")

from gi.repository import Camel, EDataServer, GLib

from .helpers import message_info_to_dict, walk_folder_info

log = logging.getLogger(__name__)

# EDS also lists RSS feeds, search folders, etc. as "Mail Account" sources.
_SKIP_BACKENDS = frozenset({"rss", "vfolder"})


class MailSession(Camel.Session):
    """Camel session: OAuth via ESource, default Camel auth for everything else."""

    __gtype_name__ = "PostMailSession"

    def __init__(self, registry: EDataServer.SourceRegistry, **kwargs):
        super().__init__(**kwargs)
        self._registry = registry

    def _credential_source(self, service) -> EDataServer.Source | None:
        """Account ESource for a Camel service (matches Evolution's EMailSession)."""
        return self._registry.ref_source(service.get_uid())

    def do_get_oauth2_access_token_sync(self, service, cancellable):
        """OAuth2 for Gmail, Microsoft 365, etc. (via GOA / ESource)."""
        source = self._credential_source(service)
        if source is None:
            return False, "", 0
        try:
            ok, token, expires_in = source.get_oauth2_access_token_sync(cancellable)
            if ok and token:
                return True, token, expires_in or 0
        except Exception:
            log.exception("OAuth2 failed for %s", service.get_uid())
        return False, "", 0


@dataclass
class MailAccount:
    uid: str
    name: str
    email: str | None
    backend: str | None


@dataclass
class MailService:
    """Thin wrapper around EDS + Camel for the GTK UI."""

    registry: EDataServer.SourceRegistry
    _session: Camel.Session | None = field(default=None, init=False)
    _stores: dict[str, Camel.Store] = field(default_factory=dict, init=False)

    @classmethod
    def connect(cls) -> MailService:
        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            raise RuntimeError(
                "Could not connect to evolution-source-registry. "
                "Is Evolution Data Server installed and your session running?"
            )
        return cls(registry=registry)

    def list_accounts(self) -> list[MailAccount]:
        accounts: list[MailAccount] = []
        for source in self.registry.list_enabled("Mail Account"):
            mail_ext = source.get_extension("Mail Account")
            backend = mail_ext.get_backend_name()
            if backend in _SKIP_BACKENDS:
                continue
            email = None
            identity_uid = mail_ext.get_identity_uid()
            if identity_uid:
                identity = self.registry.ref_source(identity_uid)
                if identity and identity.has_extension("Mail Identity"):
                    ident = identity.get_extension("Mail Identity")
                    email = ident.get_address()
            accounts.append(
                MailAccount(
                    uid=source.get_uid(),
                    name=source.get_display_name(),
                    email=email,
                    backend=backend,
                )
            )

        order = {"microsoft365": 0, "ews": 1, "imapx": 2, "imap": 3, "pop3": 4}
        accounts.sort(key=lambda a: (order.get(a.backend or "", 99), a.name))
        return accounts

    def _ensure_session(self) -> Camel.Session:
        if self._session is not None:
            self._session.set_online(True)
            return self._session

        user_data = os.path.expanduser("~/.local/share/evolution")
        user_cache = os.path.expanduser("~/.cache/evolution")
        Camel.init(user_data, False)

        self._session = MailSession(
            self.registry,
            user_data_dir=user_data,
            user_cache_dir=user_cache,
            online=True,
        )
        return self._session

    def get_store(self, account_uid: str) -> Camel.Store:
        if account_uid in self._stores:
            store = self._stores[account_uid]
            if store.get_connection_status() == Camel.ServiceConnectionStatus.CONNECTED:
                if isinstance(store, Camel.OfflineStore) and not store.get_online():
                    store.set_online_sync(True, None)
                return store
            del self._stores[account_uid]

        source = self.registry.ref_source(account_uid)
        if source is None:
            raise ValueError(f"Unknown mail account: {account_uid}")

        session = self._ensure_session()
        mail_ext = source.get_extension("Mail Account")
        service = session.add_service(
            account_uid, mail_ext.get_backend_name(), Camel.ProviderType.STORE
        )
        if service is None:
            raise RuntimeError(f"Could not create mail store for {account_uid}")

        source.camel_configure_service(service)
        store = service

        if isinstance(store, Camel.OfflineStore):
            store.set_online_sync(True, None)
        else:
            store.connect_sync(None)

        self._stores[account_uid] = store
        return store

    def list_folders(self, account_uid: str) -> list[dict]:
        store = self.get_store(account_uid)
        root = store.get_folder_info_sync(
            None, Camel.StoreGetFolderInfoFlags.RECURSIVE, None
        )
        folders: list[dict] = []
        if root is not None:
            walk_folder_info(root, folders)
        return [f for f in folders if f.get("full_name")]

    @staticmethod
    def pick_default_account(accounts: list[MailAccount]) -> MailAccount | None:
        preferred = ("microsoft365", "ews", "imapx", "imap", "pop3")
        for backend in preferred:
            for account in accounts:
                if account.backend == backend:
                    return account
        return accounts[0] if accounts else None

    def list_messages(
        self, account_uid: str, folder_name: str, limit: int = 50
    ) -> list[dict]:
        store = self.get_store(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        folder.refresh_info_sync(None)
        uids = folder.get_uids()
        if uids is None:
            return []

        uid_list = sorted(
            (str(u) for u in uids),
            key=lambda u: int(u) if u.isdigit() else 0,
            reverse=True,
        )[:limit]

        messages: list[dict] = []
        for uid in uid_list:
            info = folder.get_message_info(uid)
            if info is not None:
                messages.append(message_info_to_dict(info))
        return messages

    def read_message(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict:
        from .helpers import extract_plain_body

        store = self.get_store(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        mime = folder.get_message_sync(message_uid, None)
        if mime is None:
            raise ValueError(f"Message not found: {message_uid}")

        info = folder.get_message_info(message_uid)
        result = message_info_to_dict(info) if info else {"uid": message_uid}
        result["body_plain"] = extract_plain_body(mime)
        return result

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        for folder in folders:
            name = (folder.get("full_name") or "").upper()
            if name in ("INBOX", "INBOX/"):
                return folder["full_name"]
        for folder in folders:
            display = (folder.get("display_name") or "").lower()
            if display == "inbox":
                return folder.get("full_name")
        return folders[0]["full_name"] if folders else None
