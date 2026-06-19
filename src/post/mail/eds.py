# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later
#
# EDS/Camel glue derived from EvolutionMCP (MIT) — see LICENSES/MIT-EvolutionMCP.txt

"""EDS SourceRegistry + Camel session."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")

from gi.repository import Camel, EDataServer, GLib

from .helpers import (
    message_info_to_dict,
    paginate_messages,
    sort_messages_newest_first,
    walk_folder_info,
)
from .auth import PasswordPromptCallback, authenticate_service_sync
from .folders import guess_inbox_name

log = logging.getLogger(__name__)

# EDS also lists RSS feeds, search folders, etc. as "Mail Account" sources.
_SKIP_BACKENDS = frozenset({"rss", "vfolder"})
DEFAULT_MESSAGE_PAGE_SIZE = 50


@dataclass
class _FolderMessageIndex:
    messages: list[dict]
    unread: int
    total: int


class MailSession(Camel.Session):
    """Camel session: OAuth via ESource, password auth for IMAP."""

    __gtype_name__ = "PostMailSession"

    def __init__(
        self,
        registry: EDataServer.SourceRegistry,
        password_prompt: PasswordPromptCallback | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._registry = registry
        self._password_prompt = password_prompt

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback

    def do_get_filter_driver(self, type, for_folder=None):
        """Required when Camel parses MIME (e.g. reading messages)."""
        return Camel.FilterDriver.new(self)

    def _credential_source(self, service) -> EDataServer.Source | None:
        """Account ESource for a Camel service (matches Evolution's EMailSession)."""
        return self._registry.ref_source(service.get_uid())

    def do_authenticate_sync(self, service, mechanism=None, cancellable=None):
        """Password auth for IMAP; OAuth via do_get_oauth2_access_token_sync."""
        if mechanism == "XOAUTH2":
            result = service.authenticate_sync(mechanism, cancellable)
            return result == Camel.AuthenticationResult.ACCEPTED

        source = self._credential_source(service)
        if source is None:
            return False

        if authenticate_service_sync(
            service,
            source,
            self._registry,
            mechanism,
            cancellable,
            self._password_prompt,
        ):
            return True

        log.warning(
            "Authentication failed for %s (mechanism=%s)",
            source.get_display_name(),
            mechanism,
        )
        return False

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

    @property
    def display_label(self) -> str:
        return self.email or self.name


@dataclass
class MailService:
    """Thin wrapper around EDS + Camel for the GTK UI."""

    registry: EDataServer.SourceRegistry
    _session: Camel.Session | None = field(default=None, init=False)
    _stores: dict[str, Camel.Store] = field(default_factory=dict, init=False)
    _folder_indexes: dict[tuple[str, str], _FolderMessageIndex] = field(
        default_factory=dict, init=False
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _password_prompt: PasswordPromptCallback | None = field(default=None, init=False)

    @classmethod
    def connect(cls) -> MailService:
        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            raise RuntimeError(
                "Could not connect to evolution-source-registry. "
                "Is Evolution Data Server installed and your session running?"
            )
        return cls(registry=registry)

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback
        if isinstance(self._session, MailSession):
            self._session.set_password_prompt(callback)

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
            password_prompt=self._password_prompt,
            user_data_dir=user_data,
            user_cache_dir=user_cache,
            online=True,
        )
        return self._session

    def get_store(self, account_uid: str) -> Camel.Store:
        with self._lock:
            return self._get_store_unlocked(account_uid)

    def _get_store_unlocked(self, account_uid: str) -> Camel.Store:
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
        with self._lock:
            return self._list_folders_unlocked(account_uid)

    def _list_folders_unlocked(self, account_uid: str) -> list[dict]:
        store = self._get_store_unlocked(account_uid)
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
        self,
        account_uid: str,
        folder_name: str,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
    ) -> list[dict]:
        messages, _unread, _total, _has_more = self.list_messages_page(
            account_uid, folder_name, offset=0, limit=limit
        )
        return messages

    def list_messages_page(
        self,
        account_uid: str,
        folder_name: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
    ) -> tuple[list[dict], int, int, bool]:
        with self._lock:
            return self._list_messages_page_unlocked(
                account_uid, folder_name, offset=offset, limit=limit
            )

    def list_messages_with_stats(
        self,
        account_uid: str,
        folder_name: str,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
    ) -> tuple[list[dict], int, int]:
        messages, unread, total, _has_more = self.list_messages_page(
            account_uid, folder_name, offset=0, limit=limit
        )
        return messages, unread, total

    def _list_messages_page_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[dict], int, int, bool]:
        key = (account_uid, folder_name)
        if offset == 0:
            index = self._build_folder_index_unlocked(account_uid, folder_name)
            self._folder_indexes[key] = index
        else:
            index = self._folder_indexes.get(key)
            if index is None:
                index = self._build_folder_index_unlocked(account_uid, folder_name)
                self._folder_indexes[key] = index

        page, has_more = paginate_messages(index.messages, offset, limit)
        return page, index.unread, index.total, has_more

    def _build_folder_index_unlocked(
        self, account_uid: str, folder_name: str
    ) -> _FolderMessageIndex:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        folder.refresh_info_sync(None)
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()

        uids = folder.get_uids()
        if uids is None:
            return _FolderMessageIndex(messages=[], unread=unread, total=total)

        messages: list[dict] = []
        for uid in uids:
            info = folder.get_message_info(str(uid))
            if info is not None:
                messages.append(message_info_to_dict(info))

        return _FolderMessageIndex(
            messages=sort_messages_newest_first(messages),
            unread=unread,
            total=total,
        )

    def read_message(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict:
        with self._lock:
            return self._read_message_unlocked(account_uid, folder_name, message_uid)

    def _read_message_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict:
        from .helpers import extract_attachments, extract_message_bodies

        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        mime = folder.get_message_sync(message_uid, None)
        if mime is None:
            raise ValueError(f"Message not found: {message_uid}")

        info = folder.get_message_info(message_uid)
        result = message_info_to_dict(info) if info else {"uid": message_uid}
        bodies = extract_message_bodies(mime)
        result["body_plain"] = bodies["plain"]
        result["body_html"] = bodies["html"]
        result["attachments"] = extract_attachments(mime)
        return result

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        return guess_inbox_name(folders)
