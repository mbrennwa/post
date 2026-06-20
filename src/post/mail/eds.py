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
from typing import Any

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
from .compose import addresses_to_internet_address, build_plain_mime_message
from .folders import find_folder_by_type, guess_inbox_name

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
    identity_uid: str | None = None
    from_name: str | None = None
    from_address: str | None = None
    transport_uid: str | None = None

    @property
    def display_label(self) -> str:
        return self.email or self.name

    @property
    def from_label(self) -> str:
        if self.from_name and self.from_address:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address or self.email or self.name


@dataclass
class MailService:
    """Thin wrapper around EDS + Camel for the GTK UI."""

    registry: EDataServer.SourceRegistry
    _session: Camel.Session | None = field(default=None, init=False)
    _stores: dict[str, Camel.Store] = field(default_factory=dict, init=False)
    _transports: dict[str, Camel.Transport] = field(default_factory=dict, init=False)
    _accounts_by_uid: dict[str, MailAccount] = field(default_factory=dict, init=False)
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
            from_name = None
            from_address = None
            transport_uid = None
            if identity_uid:
                identity = self.registry.ref_source(identity_uid)
                if identity and identity.has_extension("Mail Identity"):
                    ident = identity.get_extension("Mail Identity")
                    from_name = ident.get_name()
                    from_address = ident.get_address()
                    email = from_address
                    if identity.has_extension("Mail Submission"):
                        submission = identity.get_extension("Mail Submission")
                        transport_uid = submission.get_transport_uid()
            accounts.append(
                MailAccount(
                    uid=source.get_uid(),
                    name=source.get_display_name(),
                    email=email,
                    backend=backend,
                    identity_uid=identity_uid,
                    from_name=from_name,
                    from_address=from_address,
                    transport_uid=transport_uid,
                )
            )

        order = {"microsoft365": 0, "ews": 1, "imapx": 2, "imap": 3, "pop3": 4}
        accounts.sort(key=lambda a: (order.get(a.backend or "", 99), a.name))
        self._accounts_by_uid = {account.uid: account for account in accounts}
        return accounts

    def get_account(self, account_uid: str) -> MailAccount:
        with self._lock:
            account = self._accounts_by_uid.get(account_uid)
            if account is not None:
                return account
            for candidate in self.list_accounts():
                if candidate.uid == account_uid:
                    return candidate
            raise ValueError(f"Unknown mail account: {account_uid}")

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

    def _get_transport_unlocked(self, account_uid: str) -> Camel.Transport:
        account = self.get_account(account_uid)
        transport_uid = account.transport_uid
        if not transport_uid:
            raise ValueError("No mail transport configured for this account")

        if transport_uid in self._transports:
            transport = self._transports[transport_uid]
            if (
                transport.get_connection_status()
                == Camel.ServiceConnectionStatus.CONNECTED
            ):
                return transport
            del self._transports[transport_uid]

        transport_source = self.registry.ref_source(transport_uid)
        if transport_source is None:
            raise ValueError(f"Unknown mail transport: {transport_uid}")

        session = self._ensure_session()
        mail_transport = transport_source.get_extension("Mail Transport")
        backend = mail_transport.get_backend_name()

        service = session.ref_service(transport_uid)
        if service is None:
            service = session.add_service(
                transport_uid, backend, Camel.ProviderType.TRANSPORT
            )
        if service is None:
            raise RuntimeError(f"Could not create mail transport for {account_uid}")

        transport_source.camel_configure_service(service)
        transport = service

        if hasattr(Camel, "OfflineTransport") and isinstance(
            transport, Camel.OfflineTransport
        ):
            transport.set_online_sync(True, None)
        else:
            transport.connect_sync(None)

        self._transports[transport_uid] = transport
        return transport

    def send_message(
        self,
        account_uid: str,
        *,
        to: list[str],
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> None:
        with self._lock:
            self._send_message_unlocked(
                account_uid,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
                references=references,
            )

    def _send_message_unlocked(
        self,
        account_uid: str,
        *,
        to: list[str],
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,
        in_reply_to: str | None,
        references: str | None,
    ) -> None:
        account = self.get_account(account_uid)
        from_address = account.from_address or account.email
        if not from_address:
            raise ValueError("No From address configured for this account")

        message = build_plain_mime_message(
            from_name=account.from_name,
            from_address=from_address,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )

        sender = Camel.InternetAddress.new()
        sender.add(account.from_name or "", from_address)

        recipients = Camel.InternetAddress.new()
        for group in (to, cc or [], bcc or []):
            addrs = addresses_to_internet_address(group)
            if addrs is not None:
                recipients.cat(addrs)
        if recipients.length() == 0:
            raise ValueError("At least one recipient is required")

        transport = self._get_transport_unlocked(account_uid)
        try:
            ok, _user_stop = transport.send_to_sync(
                message, sender, recipients, None
            )
        finally:
            try:
                transport.disconnect_sync(True, None)
            except Exception:
                log.exception("Failed to disconnect transport after send")
            self._transports.pop(account.transport_uid or "", None)

        if not ok:
            raise RuntimeError("Could not send message")

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

    def read_attachment_data(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        attachment_index: int,
    ) -> tuple[str, bytes]:
        with self._lock:
            return self._read_attachment_data_unlocked(
                account_uid, folder_name, message_uid, attachment_index
            )

    def toggle_message_seen(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        with self._lock:
            return self._toggle_message_seen_unlocked(
                account_uid, folder_name, message_uid
            )

    def toggle_message_flagged(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        with self._lock:
            return self._toggle_message_flagged_unlocked(
                account_uid, folder_name, message_uid
            )

    def toggle_messages_seen(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            return self._toggle_messages_seen_unlocked(
                account_uid, folder_name, message_uids
            )

    def toggle_messages_flagged(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            return self._toggle_messages_flagged_unlocked(
                account_uid, folder_name, message_uids
            )

    def move_messages_to_trash(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            return self._move_messages_to_trash_unlocked(
                account_uid, folder_name, message_uids
            )

    def archive_messages(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        with self._lock:
            return self._archive_messages_unlocked(
                account_uid, folder_name, message_uids
            )

    def move_messages(
        self,
        account_uid: str,
        source_folder: str,
        destination_folder: str,
        message_uids: list[str],
    ) -> dict[str, Any]:
        with self._lock:
            store = self._get_store_unlocked(account_uid)
            dest = store.get_folder_sync(destination_folder, 0, None)
            if dest is None:
                raise ValueError(f"Folder not found: {destination_folder}")
            return self._transfer_messages_unlocked(
                account_uid, source_folder, message_uids, dest
            )

    def mark_message_read(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> tuple[int, int]:
        with self._lock:
            return self._mark_message_read_unlocked(
                account_uid, folder_name, message_uid
            )

    def _read_message_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict:
        from .helpers import extract_attachments, extract_message_bodies

        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        info = folder.get_message_info(message_uid)
        was_unread = info is not None and not (
            info.get_flags() & Camel.MessageFlags.SEEN
        )

        mime = folder.get_message_sync(message_uid, None)
        if mime is None:
            raise ValueError(f"Message not found: {message_uid}")

        result = message_info_to_dict(info) if info else {"uid": message_uid}
        bodies = extract_message_bodies(mime)
        result["body_plain"] = bodies["plain"]
        result["body_html"] = bodies["html"]
        result["attachments"] = extract_attachments(mime)
        if not result.get("message_id") and hasattr(mime, "get_message_id"):
            result["message_id"] = mime.get_message_id()
        if hasattr(mime, "get_header"):
            references = mime.get_header("References")
            if references:
                result["references"] = references

        if was_unread:
            unread, total = self._mark_message_seen_unlocked(
                folder, account_uid, folder_name, message_uid
            )
            result.setdefault("flags", {})["seen"] = True
            result["folder_unread"] = unread
            result["folder_total"] = total

        return result

    def _read_attachment_data_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        attachment_index: int,
    ) -> tuple[str, bytes]:
        from .helpers import get_attachment_data

        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        mime = folder.get_message_sync(message_uid, None)
        if mime is None:
            raise ValueError(f"Message not found: {message_uid}")

        return get_attachment_data(mime, attachment_index)

    def _mark_message_read_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> tuple[int, int]:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        info = folder.get_message_info(message_uid)
        if info is None:
            raise ValueError(f"Message not found: {message_uid}")

        if info.get_flags() & Camel.MessageFlags.SEEN:
            return folder.get_unread_message_count(), folder.get_message_count()

        return self._mark_message_seen_unlocked(
            folder, account_uid, folder_name, message_uid
        )

    def _mark_message_seen_unlocked(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> tuple[int, int]:
        """Mark a message seen without refreshing the whole folder summary."""
        self._apply_message_flags_unlocked(
            folder,
            account_uid,
            folder_name,
            message_uid,
            Camel.MessageFlags.SEEN,
            Camel.MessageFlags.SEEN,
        )
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return unread, total

    def _toggle_message_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        info = folder.get_message_info(message_uid)
        if info is None:
            raise ValueError(f"Message not found: {message_uid}")

        currently_seen = bool(info.get_flags() & Camel.MessageFlags.SEEN)
        new_seen = not currently_seen
        flag_value = Camel.MessageFlags.SEEN if new_seen else 0
        self._apply_message_flags_unlocked(
            folder,
            account_uid,
            folder_name,
            message_uid,
            Camel.MessageFlags.SEEN,
            flag_value,
        )
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "flags": {"seen": new_seen},
            "folder_unread": unread,
            "folder_total": total,
        }

    def _toggle_message_flagged_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        info = folder.get_message_info(message_uid)
        if info is None:
            raise ValueError(f"Message not found: {message_uid}")

        currently_flagged = bool(info.get_flags() & Camel.MessageFlags.FLAGGED)
        new_flagged = not currently_flagged
        flag_value = Camel.MessageFlags.FLAGGED if new_flagged else 0
        self._apply_message_flags_unlocked(
            folder,
            account_uid,
            folder_name,
            message_uid,
            Camel.MessageFlags.FLAGGED,
            flag_value,
        )
        return {"flags": {"flagged": new_flagged}}

    def _toggle_messages_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_seen = bool(info.get_flags() & Camel.MessageFlags.SEEN)
            new_seen = not currently_seen
            flag_value = Camel.MessageFlags.SEEN if new_seen else 0
            self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.SEEN,
                flag_value,
            )
            updates.append({"uid": message_uid, "flags": {"seen": new_seen}})
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "updates": updates,
            "folder_unread": unread,
            "folder_total": total,
        }

    def _toggle_messages_flagged_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_flagged = bool(info.get_flags() & Camel.MessageFlags.FLAGGED)
            new_flagged = not currently_flagged
            flag_value = Camel.MessageFlags.FLAGGED if new_flagged else 0
            self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.FLAGGED,
                flag_value,
            )
            updates.append({"uid": message_uid, "flags": {"flagged": new_flagged}})
        return {"updates": updates}

    def _move_messages_to_trash_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        store = self._get_store_unlocked(account_uid)
        trash_folder = store.get_trash_folder_sync(None)
        if trash_folder is None:
            folders = self._list_folders_unlocked(account_uid)
            trash_info = find_folder_by_type(
                folders,
                Camel.FolderInfoFlags.TYPE_TRASH,
                type_mask=Camel.FOLDER_TYPE_MASK,
                name_fallbacks=frozenset({"trash", "deleted", "bin"}),
            )
            if trash_info is None:
                raise ValueError("Trash folder not found for this account")
            trash_folder = store.get_folder_sync(trash_info["full_name"], 0, None)
            if trash_folder is None:
                raise ValueError("Trash folder not found for this account")

        return self._transfer_messages_unlocked(
            account_uid, folder_name, message_uids, trash_folder
        )

    def _archive_messages_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        store = self._get_store_unlocked(account_uid)
        folders = self._list_folders_unlocked(account_uid)
        archive_info = find_folder_by_type(
            folders,
            Camel.FolderInfoFlags.TYPE_ARCHIVE,
            type_mask=Camel.FOLDER_TYPE_MASK,
            name_fallbacks=frozenset({"archive", "archives"}),
        )
        if archive_info is None:
            raise ValueError("Archive folder not found for this account")

        archive_folder = store.get_folder_sync(archive_info["full_name"], 0, None)
        if archive_folder is None:
            raise ValueError("Archive folder not found for this account")

        return self._transfer_messages_unlocked(
            account_uid, folder_name, message_uids, archive_folder
        )

    def _transfer_messages_unlocked(
        self,
        account_uid: str,
        source_folder_name: str,
        message_uids: list[str],
        destination_folder: Camel.Folder,
    ) -> dict[str, Any]:
        if not message_uids:
            return {"moved_uids": []}

        source_folder = self._open_folder_unlocked(account_uid, source_folder_name)
        dest_name = destination_folder.get_full_name()
        if dest_name and dest_name == source_folder_name:
            raise ValueError("Messages are already in that folder")

        source_messages = self._message_dicts_for_uids_unlocked(
            source_folder, message_uids
        )

        ok, transferred = source_folder.transfer_messages_to_sync(
            message_uids, destination_folder, True, None
        )
        if not ok:
            raise RuntimeError("Could not move messages")

        # Camel returns destination UIDs; the UI and cache use source UIDs.
        moved_uids = list(message_uids)
        destination_uids = self._camel_uid_list(transferred)
        source_folder.refresh_info_sync(None)
        destination_folder.refresh_info_sync(None)
        if not destination_uids:
            destination_uids = self._find_moved_uids_in_folder_unlocked(
                destination_folder, source_messages
            )

        source_unread = source_folder.get_unread_message_count()
        source_total = source_folder.get_message_count()
        self._remove_messages_from_cache(
            account_uid, source_folder_name, moved_uids, source_unread, source_total
        )
        self._invalidate_folder_index(account_uid, dest_name)

        dest_unread = destination_folder.get_unread_message_count()
        dest_total = destination_folder.get_message_count()
        if dest_name:
            self._update_cached_folder_counts(
                account_uid, dest_name, dest_unread, dest_total
            )

        return {
            "moved_uids": moved_uids,
            "destination_uids": destination_uids,
            "source_folder": source_folder_name,
            "source_folder_unread": source_unread,
            "source_folder_total": source_total,
            "destination_folder": dest_name,
            "destination_folder_unread": dest_unread,
            "destination_folder_total": dest_total,
        }

    def _open_folder_unlocked(
        self, account_uid: str, folder_name: str
    ) -> Camel.Folder:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")
        return folder

    @staticmethod
    def _camel_uid_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        try:
            length = value.get_length()
            return [str(value.get_nth(index)) for index in range(length)]
        except (AttributeError, TypeError):
            pass
        try:
            return [str(uid) for uid in value]
        except TypeError:
            return [str(value)]

    def _message_dicts_for_uids_unlocked(
        self, folder: Camel.Folder, message_uids: list[str]
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is not None:
                messages.append(message_info_to_dict(info))
        return messages

    def _find_moved_uids_in_folder_unlocked(
        self, folder: Camel.Folder, source_messages: list[dict[str, Any]]
    ) -> list[str]:
        if not source_messages:
            return []

        fingerprints = {
            (
                message.get("subject") or "",
                message.get("from") or "",
                message.get("sort_date") or 0,
            )
            for message in source_messages
        }
        found: list[str] = []
        uids = folder.get_uids()
        if uids is None:
            return []

        for uid in uids:
            info = folder.get_message_info(str(uid))
            if info is None:
                continue
            message = message_info_to_dict(info)
            fingerprint = (
                message.get("subject") or "",
                message.get("from") or "",
                message.get("sort_date") or 0,
            )
            if fingerprint in fingerprints:
                found.append(str(uid))
        return found

    def _apply_message_flags_unlocked(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        mask: int,
        value: int,
    ) -> None:
        folder.set_message_flags(message_uid, mask, value)
        if mask & Camel.MessageFlags.SEEN:
            self._update_cached_message_flags(
                account_uid,
                folder_name,
                message_uid,
                seen=bool(value & Camel.MessageFlags.SEEN),
            )
        if mask & Camel.MessageFlags.FLAGGED:
            self._update_cached_message_flags(
                account_uid,
                folder_name,
                message_uid,
                flagged=bool(value & Camel.MessageFlags.FLAGGED),
            )

    def _update_cached_message_flags(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is None:
            return
        for message in index.messages:
            if message.get("uid") == message_uid:
                flags = message.setdefault("flags", {})
                if seen is not None:
                    flags["seen"] = seen
                if flagged is not None:
                    flags["flagged"] = flagged
                break

    def _update_cached_folder_counts(
        self, account_uid: str, folder_name: str, unread: int, total: int
    ) -> None:
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is not None:
            index.unread = unread
            index.total = total

    def _remove_messages_from_cache(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        unread: int,
        total: int,
    ) -> None:
        uid_set = set(message_uids)
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is None:
            return
        index.messages = [
            message
            for message in index.messages
            if message.get("uid") not in uid_set
        ]
        index.unread = unread
        index.total = total

    def _invalidate_folder_index(
        self, account_uid: str, folder_name: str | None
    ) -> None:
        if folder_name:
            self._folder_indexes.pop((account_uid, folder_name), None)

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        return guess_inbox_name(folders)
