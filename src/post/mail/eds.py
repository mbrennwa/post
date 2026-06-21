# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later AND LicenseRef-MIT-EvolutionMCP
#
# EDS/Camel glue derived from EvolutionMCP (MIT) — see LICENSES/LicenseRef-MIT-EvolutionMCP.txt

"""EDS SourceRegistry + Camel session."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence, Callable

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")
gi.require_version("Gio", "2.0")

from gi.repository import Camel, EDataServer, GLib, Gio

from .helpers import (
    message_info_to_dict,
    message_is_read_unflagged,
    message_is_unread,
    paginate_messages,
    sort_messages_newest_first,
    walk_folder_info,
)
from .accounts import (
    BUILTIN_LOCAL_UID,
    EDS_LOCAL_DISPLAY_NAME,
    MailAccount,
    POST_LOCAL_ACCOUNT_UID,
    compose_from_accounts,
    ensure_post_local_mail_transport,
    is_builtin_local_store_empty,
    read_local_mail_config,
    should_list_local_account,
)
from .camel_util import camel_uid_list, normalize_camel_uid
from .message_flags import (
    apply_message_flags as _apply_message_flags,
    mark_message_seen as _mark_message_seen,
    persist_folder_flags as _persist_folder_flags,
)
from .local_delivery import all_recipients_local, can_deliver_locally, deliver_local_message
from .send_errors import (
    MESSAGE_QUEUED,
    SYSTEM_MAIL_EXTERNAL_RECIPIENTS,
    SendError,
    SendQueued,
    user_send_error_message,
)
from .send_queue import (
    QueuedOutboundMessage,
    enqueue_outbound_message,
    is_network_unavailable_error,
    is_queueable_network_error,
    list_queued_outbound_messages,
    load_queued_attachments,
    remove_queued_outbound_message,
)
from post.preferences import get_show_evolution_local
from .auth import PasswordPromptCallback, authenticate_service_sync
from .compose import (
    ComposeAttachment,
    addresses_to_internet_address,
    build_draft_mime_message,
    build_plain_mime_message,
    normalize_email,
)
from .correspondents import Correspondent, collect_correspondents
from .folders import (
    find_folder_by_type,
    find_trash_folder,
    folder_can_contain_messages,
    folder_name_from_uri,
    guess_inbox_name,
    is_virtual_folder,
    validate_folder_display_name,
)
from .search import MessageSearchQuery, message_matches

log = logging.getLogger(__name__)

# Limit autocomplete index size; messages are processed newest-first.
_MAX_CORRESPONDENTS = 500

# EDS also lists RSS feeds, search folders, etc. as "Mail Account" sources.
_SKIP_BACKENDS = frozenset({"rss", "vfolder"})
DEFAULT_MESSAGE_PAGE_SIZE = 50
_SEND_TIMEOUT_SECONDS = 30
# Stay below Camel IMAPx MAX_UIDSET_ITEMS (100) to avoid spurious uidset warnings
# in evolution-data-server 3.56 when batching UID MOVE/COPY commands.
_TRANSFER_MESSAGE_BATCH_SIZE = 50


@dataclass
class _FolderMessageIndex:
    messages: list[dict]
    unread: int
    total: int


def _read_unflagged_uids(index: _FolderMessageIndex) -> list[str]:
    return [
        message["uid"]
        for message in index.messages
        if message.get("uid") and message_is_read_unflagged(message)
    ]


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
    _correspondent_indexes: dict[str, list[Correspondent]] = field(
        default_factory=dict, init=False
    )
    _folder_tree_cache: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _network_available: bool = field(default=True, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _password_prompt: PasswordPromptCallback | None = field(default=None, init=False)
    _pending_mail_ops: int = field(default=0, init=False)
    _pending_mail_ops_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )

    def _enter_mail_op(self) -> None:
        with self._pending_mail_ops_cond:
            self._pending_mail_ops += 1

    def _leave_mail_op(self) -> None:
        with self._pending_mail_ops_cond:
            self._pending_mail_ops -= 1
            if self._pending_mail_ops == 0:
                self._pending_mail_ops_cond.notify_all()

    def _with_mail_op(self, operation: Callable[[], Any]) -> Any:
        self._enter_mail_op()
        try:
            with self._lock:
                return operation()
        finally:
            self._leave_mail_op()

    def wait_for_pending_mail_ops(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        with self._pending_mail_ops_cond:
            while self._pending_mail_ops > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "Timed out waiting for %d pending mail operation(s)",
                        self._pending_mail_ops,
                    )
                    return
                self._pending_mail_ops_cond.wait(timeout=remaining)

    def shutdown_sync(self) -> None:
        """Wait for in-flight mail work and flush stores before exit."""
        self.wait_for_pending_mail_ops()
        with self._lock:
            for store in self._stores.values():
                if (
                    store.get_connection_status()
                    != Camel.ServiceConnectionStatus.CONNECTED
                ):
                    continue
                try:
                    store.synchronize_sync(False, None)
                except GLib.Error:
                    log.exception("Failed to flush mail store on shutdown")

    @classmethod
    def connect(cls) -> MailService:
        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            raise RuntimeError(
                "Could not connect to evolution-source-registry. "
                "Is Evolution Data Server installed and your session running?"
            )
        ensure_post_local_mail_transport(registry)
        registry = EDataServer.SourceRegistry.new_sync(None)
        if registry is None:
            raise RuntimeError(
                "Could not reconnect to evolution-source-registry after "
                "configuring local mail transport."
            )
        return cls(registry=registry)

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback
        if isinstance(self._session, MailSession):
            self._session.set_password_prompt(callback)

    def set_network_available(self, available: bool) -> None:
        """Update Camel session/store online state from Gio.NetworkMonitor."""
        with self._lock:
            if self._network_available == available:
                return
            self._network_available = available
            if self._session is not None:
                self._session.set_online(available)
            for store in self._stores.values():
                if isinstance(store, Camel.OfflineStore):
                    try:
                        store.set_online_sync(available, None)
                    except GLib.Error:
                        log.debug(
                            "Could not set store offline=%s",
                            not available,
                            exc_info=True,
                        )

    def go_online_sync(self) -> None:
        """Bring offline stores back online and drop cached folder indexes."""
        with self._lock:
            self._network_available = True
            if self._session is not None:
                self._session.set_online(True)
            for store in self._stores.values():
                if isinstance(store, Camel.OfflineStore):
                    try:
                        store.set_online_sync(True, None)
                    except GLib.Error:
                        log.exception("Failed to bring mail store online")
            self._folder_indexes.clear()

    def reload_registry(self) -> None:
        """Reconnect to EDS and drop cached Camel services (after account changes)."""
        with self._lock:
            self._stores.clear()
            self._transports.clear()
            self._folder_indexes.clear()
            self._correspondent_indexes.clear()
            self._folder_tree_cache.clear()
            self._accounts_by_uid.clear()
            self._session = None
            registry = EDataServer.SourceRegistry.new_sync(None)
            if registry is None:
                raise RuntimeError(
                    "Could not reconnect to evolution-source-registry."
                )
            ensure_post_local_mail_transport(registry)
            registry = EDataServer.SourceRegistry.new_sync(None)
            if registry is None:
                raise RuntimeError(
                    "Could not reconnect to evolution-source-registry."
                )
            self.registry = registry

    def list_accounts(self) -> list[MailAccount]:
        accounts: list[MailAccount] = []
        evolution_local_pref = get_show_evolution_local()
        hide_empty_builtin_local = is_builtin_local_store_empty(self.registry)
        for source in self.registry.list_enabled("Mail Account"):
            uid = source.get_uid()
            if uid == BUILTIN_LOCAL_UID:
                if evolution_local_pref is False:
                    continue
                if evolution_local_pref is None and hide_empty_builtin_local:
                    continue
            mail_ext = source.get_extension("Mail Account")
            backend = mail_ext.get_backend_name()
            if backend in _SKIP_BACKENDS:
                continue
            if not should_list_local_account(source):
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

        order = {
            "microsoft365": 0,
            "ews": 1,
            "imapx": 2,
            "imap": 3,
            "pop3": 4,
            "spool": 5,
            "maildir": 6,
        }
        accounts.sort(key=lambda a: (order.get(a.backend or "", 99), a.name))
        self._accounts_by_uid = {account.uid: account for account in accounts}
        return accounts

    def list_sendable_accounts(self) -> list[MailAccount]:
        return [account for account in self.list_accounts() if account.can_send]

    @staticmethod
    def compose_from_accounts(
        sendable: list[MailAccount], preferred: MailAccount | None
    ) -> list[MailAccount]:
        return compose_from_accounts(sendable, preferred)

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
            self._session.set_online(self._network_available)
            return self._session

        user_data = os.path.expanduser("~/.local/share/evolution")
        user_cache = os.path.expanduser("~/.cache/evolution")
        Camel.init(user_data, False)

        self._session = MailSession(
            self.registry,
            password_prompt=self._password_prompt,
            user_data_dir=user_data,
            user_cache_dir=user_cache,
            online=self._network_available,
        )
        return self._session

    def get_store(self, account_uid: str) -> Camel.Store:
        with self._lock:
            return self._get_store_unlocked(account_uid)

    def get_store_for_sync(self, account_uid: str) -> Camel.Store:
        """Return a connected store for sync signal wiring."""
        return self.get_store(account_uid)

    def invalidate_folder_index(self, account_uid: str, folder_name: str) -> None:
        with self._lock:
            self._invalidate_folder_index(account_uid, folder_name)

    def _get_store_unlocked(self, account_uid: str) -> Camel.Store:
        if account_uid in self._stores:
            store = self._stores[account_uid]
            if store.get_connection_status() == Camel.ServiceConnectionStatus.CONNECTED:
                self._sync_store_online_state_unlocked(store)
                self._configure_store_settings_unlocked(store)
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

        self._sync_store_online_state_unlocked(store)

        self._configure_store_settings_unlocked(store)
        self._stores[account_uid] = store
        return store

    def _sync_store_online_state_unlocked(self, store: Camel.Store) -> None:
        if isinstance(store, Camel.OfflineStore):
            store.set_online_sync(self._network_available, None)
        elif self._network_available:
            store.connect_sync(None)

    @staticmethod
    def _configure_store_settings_unlocked(store: Camel.Store) -> None:
        settings = store.ref_settings()
        if settings is None:
            return
        set_interval = getattr(settings, "set_store_changes_interval", None)
        if callable(set_interval):
            set_interval(0)

    def _get_transport_unlocked(
        self,
        account_uid: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> Camel.Transport:
        account = self.get_account(account_uid)
        transport_uid = account.transport_uid
        if not transport_uid:
            raise ValueError("No mail transport configured for this account")

        transport_source = self.registry.ref_source(transport_uid)
        if transport_source is None:
            raise ValueError(f"Unknown mail transport: {transport_uid}")

        mail_transport = transport_source.get_extension("Mail Transport")
        expected_backend = mail_transport.get_backend_name()

        if transport_uid in self._transports:
            transport = self._transports[transport_uid]
            if (
                transport.get_connection_status()
                == Camel.ServiceConnectionStatus.CONNECTED
            ):
                return transport
            del self._transports[transport_uid]

        session = self._ensure_session()
        backend = expected_backend

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
            transport.set_online_sync(True, cancellable)
        else:
            transport.connect_sync(cancellable)

        self._transports[transport_uid] = transport
        return transport

    def flush_send_queue(self) -> int:
        """Try to send messages queued while offline. Returns count sent."""
        with self._lock:
            return self._flush_send_queue_unlocked()

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
        attachments: Sequence[ComposeAttachment] | None = None,
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
                attachments=attachments,
                from_queue=False,
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
        attachments: Sequence[ComposeAttachment] | None = None,
        from_queue: bool = False,
    ) -> None:
        account = self.get_account(account_uid)
        from_address = account.from_address or account.email
        if not from_address:
            raise ValueError("No From address configured for this account")

        compose_kwargs = {
            "from_name": account.from_name,
            "from_address": from_address,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body": body,
            "in_reply_to": in_reply_to,
            "references": references,
            "attachments": attachments,
        }
        message = build_plain_mime_message(**compose_kwargs)
        sent_message = build_plain_mime_message(**compose_kwargs)

        sender = Camel.InternetAddress.new()
        sender.add(account.from_name or "", from_address)

        recipients = Camel.InternetAddress.new()
        for group in (to, cc or [], bcc or []):
            addrs = addresses_to_internet_address(group)
            if addrs is not None:
                recipients.cat(addrs)
        if recipients.length() == 0:
            raise ValueError("At least one recipient is required")

        if account_uid == POST_LOCAL_ACCOUNT_UID:
            local_config = read_local_mail_config(self.registry)
            if local_config is not None and local_config.enabled:
                if can_deliver_locally(
                    local_config,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                ):
                    deliver_local_message(
                        local_config,
                        message,
                        envelope_from=from_address,
                    )
                    self._stores.pop(account_uid, None)
                    for key in list(self._folder_indexes):
                        if key[0] == account_uid:
                            self._folder_indexes.pop(key, None)
                    self._correspondent_indexes.pop(account_uid, None)
                    self._append_to_sent_folder_unlocked(account_uid, sent_message)
                    return
                if not all_recipients_local(
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    local_address=local_config.from_address,
                ):
                    raise SendError(SYSTEM_MAIL_EXTERNAL_RECIPIENTS)

        cancellable = Gio.Cancellable()
        timer = threading.Timer(_SEND_TIMEOUT_SECONDS, cancellable.cancel)
        timer.start()
        transport: Camel.Transport | None = None
        ok = False
        try:
            transport = self._get_transport_unlocked(account_uid, cancellable)
            ok, _user_stop = transport.send_to_sync(
                message, sender, recipients, cancellable
            )
        except GLib.Error as exc:
            network_exc: BaseException = (
                TimeoutError() if cancellable.is_cancelled() else exc
            )
            if not from_queue and is_queueable_network_error(network_exc):
                self._queue_outbound_message_unlocked(
                    account_uid=account_uid,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    body=body,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachments=attachments,
                )
                raise SendQueued(MESSAGE_QUEUED) from exc
            if cancellable.is_cancelled():
                raise SendError(user_send_error_message(TimeoutError())) from exc
            raise SendError(user_send_error_message(exc)) from exc
        finally:
            timer.cancel()
            if transport is not None:
                try:
                    transport.disconnect_sync(True, cancellable)
                except Exception:
                    log.debug("Failed to disconnect transport after send", exc_info=True)
            self._transports.pop(account.transport_uid or "", None)

        if not ok:
            raise SendError(user_send_error_message(RuntimeError("Could not send message")))

        self._append_to_sent_folder_unlocked(account_uid, sent_message)

    def _queue_outbound_message_unlocked(
        self,
        *,
        account_uid: str,
        to: list[str],
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,
        in_reply_to: str | None,
        references: str | None,
        attachments: Sequence[ComposeAttachment] | None = None,
    ) -> None:
        enqueue_outbound_message(
            QueuedOutboundMessage(
                account_uid=account_uid,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
                references=references,
            ),
            attachment_payloads=attachments,
        )

    def _flush_send_queue_unlocked(self) -> int:
        sent = 0
        for queue_id, queued in list_queued_outbound_messages():
            try:
                self._send_message_unlocked(
                    queued.account_uid,
                    to=queued.to,
                    cc=queued.cc,
                    bcc=queued.bcc,
                    subject=queued.subject,
                    body=queued.body,
                    in_reply_to=queued.in_reply_to,
                    references=queued.references,
                    attachments=load_queued_attachments(queue_id, queued),
                    from_queue=True,
                )
            except SendQueued:
                break
            except SendError as exc:
                log.warning(
                    "Queued message %s was not sent: %s",
                    queue_id,
                    exc.user_message,
                )
                break
            except Exception:
                log.exception("Failed to send queued message %s", queue_id)
                break
            else:
                remove_queued_outbound_message(queue_id)
                sent += 1
        return sent

    def _sent_folder_name_unlocked(self, account_uid: str) -> str | None:
        account = self.get_account(account_uid)
        if not account.identity_uid:
            return None

        identity = self.registry.ref_source(account.identity_uid)
        if identity is None or not identity.has_extension("Mail Submission"):
            return None

        submission = identity.get_extension("Mail Submission")
        if not submission.get_use_sent_folder():
            return None

        folder_name = folder_name_from_uri(submission.get_sent_folder())
        if folder_name:
            return folder_name

        folders = self._list_folders_unlocked(account_uid)
        sent_info = find_folder_by_type(
            folders,
            Camel.FolderInfoFlags.TYPE_OUTBOX,
            type_mask=Camel.FOLDER_TYPE_MASK,
            name_fallbacks=frozenset({"sent", "sent mail", "sent messages"}),
        )
        if sent_info is None:
            return None
        return sent_info.get("full_name")

    def _append_to_sent_folder_unlocked(
        self, account_uid: str, message: Camel.MimeMessage
    ) -> None:
        folder_name = self._sent_folder_name_unlocked(account_uid)
        if not folder_name:
            return

        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            log.warning(
                "Sent folder %r is not available for account %s",
                folder_name,
                account_uid,
            )
            return

        try:
            ok, _uid = folder.append_message_sync(message, None, None)
        except GLib.Error as exc:
            log.warning(
                "Failed to save a copy to Sent folder %r: %s",
                folder_name,
                exc.message,
            )
            return

        if not ok:
            log.warning("Could not append message to Sent folder %r", folder_name)
            return

        try:
            folder.refresh_info_sync(None)
        except GLib.Error:
            log.debug("Failed to refresh Sent folder after append", exc_info=True)
        self._invalidate_folder_index(account_uid, folder_name)

    def _drafts_folder_name_unlocked(self, account_uid: str) -> str | None:
        folders = self._list_folders_unlocked(account_uid)
        drafts_info = find_folder_by_type(
            folders,
            Camel.FolderInfoFlags.TYPE_DRAFTS,
            type_mask=Camel.FOLDER_TYPE_MASK,
            name_fallbacks=frozenset({"drafts", "draft"}),
        )
        if drafts_info is None:
            return None
        return drafts_info.get("full_name")

    def _draft_message_info(self) -> Camel.MessageInfo | None:
        draft_flag = getattr(Camel.MessageFlags, "DRAFT", None)
        if draft_flag is None:
            return None
        info = Camel.MessageInfo.new()
        info.set_flags(draft_flag, draft_flag)
        return info

    def _delete_message_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> None:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        deleted = Camel.MessageFlags.DELETED
        folder.set_message_flags(message_uid, deleted, deleted)
        folder.expunge_sync(None)
        try:
            folder.refresh_info_sync(None)
        except GLib.Error:
            log.debug("Failed to refresh folder after delete", exc_info=True)
        self._invalidate_folder_index(account_uid, folder_name)

    def _append_draft_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message: Camel.MimeMessage,
    ) -> str | None:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            raise RuntimeError(f"Drafts folder {folder_name!r} is not available")

        info = self._draft_message_info()
        try:
            result = folder.append_message_sync(message, info, None)
            if isinstance(result, tuple):
                ok, appended_uid = result
            else:
                ok = bool(result)
                appended_uid = None
        except GLib.Error as exc:
            raise RuntimeError(f"Could not save draft: {exc.message}") from exc

        if not ok:
            raise RuntimeError("Could not append message to Drafts folder")

        try:
            folder.refresh_info_sync(None)
        except GLib.Error:
            log.debug("Failed to refresh Drafts folder after append", exc_info=True)
        self._invalidate_folder_index(account_uid, folder_name)

        if appended_uid:
            return str(appended_uid)
        uids = folder.get_uids()
        if uids:
            return str(uids[-1])
        return None

    def save_draft(
        self,
        account_uid: str,
        *,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
        existing_uid: str | None = None,
        drafts_folder_name: str | None = None,
        attachments: Sequence[ComposeAttachment] | None = None,
    ) -> tuple[str, str]:
        """Save or update a draft. Returns (drafts_folder_name, message_uid)."""
        with self._lock:
            return self._save_draft_unlocked(
                account_uid,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
                references=references,
                existing_uid=existing_uid,
                drafts_folder_name=drafts_folder_name,
                attachments=attachments,
            )

    def _save_draft_unlocked(
        self,
        account_uid: str,
        *,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,
        in_reply_to: str | None,
        references: str | None,
        existing_uid: str | None,
        drafts_folder_name: str | None,
        attachments: Sequence[ComposeAttachment] | None = None,
    ) -> tuple[str, str]:
        account = self.get_account(account_uid)
        from_address = account.from_address or account.email
        if not from_address:
            raise ValueError("No From address configured for this account")

        message = build_draft_mime_message(
            from_name=account.from_name,
            from_address=from_address,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments,
        )

        folder_name = drafts_folder_name or self._drafts_folder_name_unlocked(
            account_uid
        )
        if not folder_name:
            raise RuntimeError("No Drafts folder is configured for this account")

        if existing_uid:
            self._delete_message_unlocked(account_uid, folder_name, existing_uid)

        appended_uid = self._append_draft_unlocked(account_uid, folder_name, message)
        if not appended_uid:
            raise RuntimeError("Draft was saved but its UID could not be determined")
        return folder_name, appended_uid

    def delete_draft(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> None:
        with self._lock:
            self._delete_message_unlocked(account_uid, folder_name, message_uid)

    def get_correspondents(self, account_uid: str) -> list[Correspondent]:
        with self._lock:
            cached = self._correspondent_indexes.get(account_uid)
            if cached is not None:
                return cached
            correspondents = self._build_correspondents_index_unlocked(account_uid)
            self._correspondent_indexes[account_uid] = correspondents
            return correspondents

    def _build_correspondents_index_unlocked(
        self, account_uid: str
    ) -> list[Correspondent]:
        account = self.get_account(account_uid)
        exclude_emails: set[str] = set()
        for raw in (account.from_address, account.email):
            if raw:
                exclude_emails.add(normalize_email(raw))

        folders = self._list_folders_unlocked(account_uid)
        messages: list[dict] = []
        for folder in folders:
            if not folder_can_contain_messages(folder):
                continue
            full_name = folder.get("full_name")
            index = self._build_folder_index_unlocked(account_uid, full_name)
            messages.extend(index.messages)

        messages.sort(key=lambda message: message.get("sort_date") or 0, reverse=True)
        correspondents = collect_correspondents(messages, exclude_emails=exclude_emails)
        return correspondents[:_MAX_CORRESPONDENTS]

    def list_folders(self, account_uid: str) -> list[dict]:
        with self._lock:
            return self._list_folders_unlocked(account_uid)

    def _list_folders_unlocked(self, account_uid: str) -> list[dict]:
        cached = self._folder_tree_cache.get(account_uid)
        try:
            store = self._get_store_unlocked(account_uid)
            root = store.get_folder_info_sync(
                None, Camel.StoreGetFolderInfoFlags.RECURSIVE, None
            )
            folders: list[dict] = []
            if root is not None:
                walk_folder_info(root, folders)
            result = [f for f in folders if f.get("full_name")]
            self._folder_tree_cache[account_uid] = result
            return result
        except GLib.Error as exc:
            if cached is not None and is_network_unavailable_error(exc):
                log.debug(
                    "Using cached folder list for account %s while offline",
                    account_uid,
                )
                return cached
            raise

    def get_folder_stats(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        """Return live (unread, total) counts by opening the folder."""
        with self._lock:
            return self._get_folder_stats_unlocked(account_uid, folder_name)

    def create_folder(
        self,
        account_uid: str,
        parent_folder_name: str | None,
        display_name: str,
    ) -> str:
        with self._lock:
            return self._create_folder_unlocked(
                account_uid, parent_folder_name, display_name
            )

    def rename_folder(
        self, account_uid: str, folder_name: str, new_display_name: str
    ) -> str:
        with self._lock:
            return self._rename_folder_unlocked(
                account_uid, folder_name, new_display_name
            )

    def delete_folder(self, account_uid: str, folder_name: str) -> None:
        with self._lock:
            self._delete_folder_unlocked(account_uid, folder_name)

    def empty_folder(self, account_uid: str, folder_name: str) -> dict[str, Any]:
        with self._lock:
            return self._empty_folder_unlocked(account_uid, folder_name)

    def archive_read_messages(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        with self._lock:
            return self._archive_read_messages_unlocked(account_uid, folder_name)

    def count_read_unflagged_messages(
        self, account_uid: str, folder_name: str
    ) -> int:
        with self._lock:
            return self._count_read_unflagged_messages_unlocked(
                account_uid, folder_name
            )

    def archive_read_unflagged_messages(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        with self._lock:
            return self._archive_read_unflagged_messages_unlocked(
                account_uid, folder_name
            )

    def _create_folder_unlocked(
        self,
        account_uid: str,
        parent_folder_name: str | None,
        display_name: str,
    ) -> str:
        account = self.get_account(account_uid)
        if account.backend == "spool":
            raise ValueError("Folder creation is not supported for spool accounts")
        cleaned = validate_folder_display_name(display_name)
        store = self._get_store_unlocked(account_uid)
        info = store.create_folder_sync(parent_folder_name, cleaned, None)
        if info is None:
            raise RuntimeError("Could not create folder")
        full_name = info.get_full_name() if hasattr(info, "get_full_name") else cleaned
        if not full_name:
            full_name = (
                f"{parent_folder_name}/{cleaned}"
                if parent_folder_name
                else cleaned
            )
        self._invalidate_account_folder_tree(account_uid)
        return str(full_name)

    def _rename_folder_unlocked(
        self, account_uid: str, folder_name: str, new_display_name: str
    ) -> str:
        account = self.get_account(account_uid)
        if account.backend == "spool":
            raise ValueError("Folder rename is not supported for spool accounts")
        cleaned = validate_folder_display_name(new_display_name)
        if folder_name == cleaned or folder_name.endswith(f"/{cleaned}"):
            return folder_name
        parent = folder_name.rsplit("/", 1)[0] if "/" in folder_name else None
        new_full_name = f"{parent}/{cleaned}" if parent else cleaned
        store = self._get_store_unlocked(account_uid)
        result = store.rename_folder_sync(folder_name, new_full_name, None)
        if result is None:
            raise RuntimeError("Could not rename folder")
        self._invalidate_account_folder_tree(account_uid, folder_name, new_full_name)
        return new_full_name

    def _delete_folder_unlocked(self, account_uid: str, folder_name: str) -> None:
        account = self.get_account(account_uid)
        if account.backend == "spool":
            raise ValueError("Folder deletion is not supported for spool accounts")
        store = self._get_store_unlocked(account_uid)
        if not store.delete_folder_sync(folder_name, None):
            raise RuntimeError("Could not delete folder")
        self._invalidate_account_folder_tree(account_uid, folder_name)

    def _empty_folder_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        uids = camel_uid_list(folder.get_uids())
        deleted = Camel.MessageFlags.DELETED
        batch_size = 50
        for offset in range(0, len(uids), batch_size):
            batch = uids[offset : offset + batch_size]
            for message_uid in batch:
                folder.set_message_flags(message_uid, deleted, deleted)
            if batch:
                folder.expunge_sync(None)
        folder.refresh_info_sync(None)
        self._invalidate_folder_index(account_uid, folder_name)
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "removed_count": len(uids),
            "folder_unread": unread,
            "folder_total": total,
        }

    def _archive_read_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        index = self._build_folder_index_unlocked(account_uid, folder_name)
        read_uids = [
            message["uid"]
            for message in index.messages
            if message.get("uid") and not message_is_unread(message)
        ]
        if not read_uids:
            return {
                "archived_count": 0,
                "source_folder_unread": index.unread,
                "source_folder_total": index.total,
            }
        result = self._archive_messages_unlocked(
            account_uid, folder_name, read_uids
        )
        result["archived_count"] = len(read_uids)
        return result

    def _count_read_unflagged_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> int:
        index = self._build_folder_index_unlocked(account_uid, folder_name)
        return len(_read_unflagged_uids(index))

    def _archive_read_unflagged_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        index = self._build_folder_index_unlocked(account_uid, folder_name)
        uids = _read_unflagged_uids(index)
        if not uids:
            return {
                "archived_count": 0,
                "source_folder_unread": index.unread,
                "source_folder_total": index.total,
            }
        result = self._archive_messages_unlocked(account_uid, folder_name, uids)
        result["archived_count"] = len(uids)
        return result

    def _invalidate_account_folder_tree(
        self,
        account_uid: str,
        *old_folder_names: str,
    ) -> None:
        for key in list(self._folder_indexes):
            if key[0] == account_uid:
                self._folder_indexes.pop(key, None)
        for old_name in old_folder_names:
            self._folder_indexes.pop((account_uid, old_name), None)
        self._correspondent_indexes.pop(account_uid, None)

    def _get_folder_stats_unlocked(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")
        folder.refresh_info_sync(None)
        return folder.get_unread_message_count(), folder.get_message_count()

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
        sync: bool = True,
    ) -> tuple[list[dict], int, int, bool]:
        with self._lock:
            return self._list_messages_page_unlocked(
                account_uid,
                folder_name,
                offset=offset,
                limit=limit,
                sync=sync,
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

    def search_messages_page(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        offset: int = 0,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
        sync: bool = True,
    ) -> tuple[list[dict], int, int, bool]:
        with self._lock:
            return self._search_messages_page_unlocked(
                account_uid,
                folder_name,
                query,
                offset=offset,
                limit=limit,
                sync=sync,
            )

    def _search_messages_page_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        offset: int,
        limit: int,
        sync: bool,
    ) -> tuple[list[dict], int, int, bool]:
        key = (account_uid, folder_name)
        index = self._folder_indexes.get(key)
        if index is None or sync:
            index = self._build_folder_index_unlocked(
                account_uid, folder_name, sync=sync
            )
            self._folder_indexes[key] = index

        filtered = [msg for msg in index.messages if message_matches(msg, query)]
        page, has_more = paginate_messages(filtered, offset, limit)
        match_count = len(filtered)
        return page, match_count, match_count, has_more

    def _list_messages_page_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        offset: int,
        limit: int,
        sync: bool,
    ) -> tuple[list[dict], int, int, bool]:
        key = (account_uid, folder_name)
        if offset == 0:
            if sync:
                index = self._build_folder_index_unlocked(
                    account_uid, folder_name, sync=True
                )
                self._folder_indexes[key] = index
            else:
                index = self._folder_indexes.get(key)
                if index is None:
                    index = self._build_folder_index_unlocked(
                        account_uid, folder_name, sync=False
                    )
                    self._folder_indexes[key] = index
        else:
            index = self._folder_indexes.get(key)
            if index is None:
                index = self._build_folder_index_unlocked(
                    account_uid, folder_name, sync=sync
                )
                self._folder_indexes[key] = index

        page, has_more = paginate_messages(index.messages, offset, limit)
        return page, index.unread, index.total, has_more

    def _is_missing_folder_error(self, exc: GLib.Error) -> bool:
        return exc.matches(Camel.store_error_quark(), Camel.StoreError.NO_FOLDER)

    def _open_folder_unlocked(
        self, account_uid: str, folder_name: str
    ) -> Camel.Folder | None:
        store = self._get_store_unlocked(account_uid)
        try:
            return store.get_folder_sync(folder_name, 0, None)
        except GLib.Error as exc:
            if self._is_missing_folder_error(exc):
                log.debug(
                    "Skipping unavailable folder %r for account %s",
                    folder_name,
                    account_uid,
                )
                return None
            raise

    def _require_folder_unlocked(
        self, account_uid: str, folder_name: str
    ) -> Camel.Folder:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")
        return folder

    def _build_folder_index_unlocked(
        self, account_uid: str, folder_name: str, *, sync: bool = True
    ) -> _FolderMessageIndex:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            return _FolderMessageIndex(messages=[], unread=0, total=0)

        try:
            if sync:
                folder.refresh_info_sync(None)
            unread = folder.get_unread_message_count()
            total = folder.get_message_count()

            uids = folder.get_uids()
            if uids is None:
                return _FolderMessageIndex(messages=[], unread=unread, total=total)

            messages: list[dict] = []
            for uid in uids:
                info = folder.get_message_info(str(uid))
                if info is None:
                    continue
                try:
                    messages.append(message_info_to_dict(info))
                except (OSError, OverflowError, ValueError):
                    log.debug(
                        "Skipping message %r in %r due to invalid metadata",
                        uid,
                        folder_name,
                        exc_info=True,
                    )

            return _FolderMessageIndex(
                messages=sort_messages_newest_first(messages),
                unread=unread,
                total=total,
            )
        except GLib.Error as exc:
            if self._is_missing_folder_error(exc):
                log.debug(
                    "Skipping unavailable folder %r for account %s",
                    folder_name,
                    account_uid,
                )
                return _FolderMessageIndex(messages=[], unread=0, total=0)
            raise

    def read_message(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        *,
        mark_seen: bool = True,
    ) -> dict:
        return self._with_mail_op(
            lambda: self._read_message_unlocked(
                account_uid, folder_name, message_uid, mark_seen=mark_seen
            )
        )

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
        return self._with_mail_op(
            lambda: self._toggle_message_seen_unlocked(
                account_uid, folder_name, message_uid
            )
        )

    def toggle_message_flagged(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        return self._with_mail_op(
            lambda: self._toggle_message_flagged_unlocked(
                account_uid, folder_name, message_uid
            )
        )

    def set_messages_seen(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        seen: bool,
    ) -> dict[str, Any]:
        return self._with_mail_op(
            lambda: self._set_messages_seen_unlocked(
                account_uid, folder_name, message_uids, seen=seen
            )
        )

    def set_messages_flagged(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        flagged: bool,
    ) -> dict[str, Any]:
        return self._with_mail_op(
            lambda: self._set_messages_flagged_unlocked(
                account_uid, folder_name, message_uids, flagged=flagged
            )
        )

    def toggle_messages_seen(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return self._with_mail_op(
            lambda: self._toggle_messages_seen_unlocked(
                account_uid, folder_name, message_uids
            )
        )

    def toggle_messages_flagged(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return self._with_mail_op(
            lambda: self._toggle_messages_flagged_unlocked(
                account_uid, folder_name, message_uids
            )
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
        return self._with_mail_op(
            lambda: self._mark_message_read_unlocked(
                account_uid, folder_name, message_uid
            )
        )

    def _read_message_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        *,
        mark_seen: bool = True,
    ) -> dict:
        from .helpers import (
            extract_attachments,
            extract_inline_images,
            extract_message_bodies,
        )

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
        result["inline_images"] = extract_inline_images(mime)
        if not result.get("message_id") and hasattr(mime, "get_message_id"):
            result["message_id"] = mime.get_message_id()
        if hasattr(mime, "get_header"):
            references = mime.get_header("References")
            if references:
                result["references"] = references

        if was_unread and mark_seen:
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
        return _mark_message_seen(
            folder,
            message_uid,
            on_seen_changed=lambda seen: self._update_cached_message_flags(
                account_uid, folder_name, message_uid, seen=seen
            ),
            update_folder_counts=lambda unread, total: (
                self._update_cached_folder_counts(
                    account_uid, folder_name, unread, total
                )
            ),
            persist_uids=lambda uids: self._persist_message_flag_changes_unlocked(
                account_uid, folder, uids
            ),
        )

    def _toggle_message_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        info = folder.get_message_info(message_uid)
        if info is None:
            raise ValueError(f"Message not found: {message_uid}")

        currently_seen = bool(info.get_flags() & Camel.MessageFlags.SEEN)
        new_seen = not currently_seen
        flag_value = Camel.MessageFlags.SEEN if new_seen else 0
        changed = self._apply_message_flags_unlocked(
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
        if changed:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, [message_uid]
            )
        return {
            "flags": {"seen": new_seen},
            "folder_unread": unread,
            "folder_total": total,
        }

    def _toggle_message_flagged_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        info = folder.get_message_info(message_uid)
        if info is None:
            raise ValueError(f"Message not found: {message_uid}")

        currently_flagged = bool(info.get_flags() & Camel.MessageFlags.FLAGGED)
        new_flagged = not currently_flagged
        flag_value = Camel.MessageFlags.FLAGGED if new_flagged else 0
        changed = self._apply_message_flags_unlocked(
            folder,
            account_uid,
            folder_name,
            message_uid,
            Camel.MessageFlags.FLAGGED,
            flag_value,
        )
        if changed:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, [message_uid]
            )
        return {"flags": {"flagged": new_flagged}}

    def _set_messages_seen_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        seen: bool,
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_seen = bool(info.get_flags() & Camel.MessageFlags.SEEN)
            if currently_seen == seen:
                updates.append({"uid": message_uid, "flags": {"seen": seen}})
                continue
            flag_value = Camel.MessageFlags.SEEN if seen else 0
            if self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.SEEN,
                flag_value,
            ):
                changed_uids.append(message_uid)
            updates.append({"uid": message_uid, "flags": {"seen": seen}})
        if changed_uids:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, changed_uids
            )
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "updates": updates,
            "folder_unread": unread,
            "folder_total": total,
        }

    def _set_messages_flagged_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        flagged: bool,
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_flagged = bool(info.get_flags() & Camel.MessageFlags.FLAGGED)
            if currently_flagged == flagged:
                updates.append({"uid": message_uid, "flags": {"flagged": flagged}})
                continue
            flag_value = Camel.MessageFlags.FLAGGED if flagged else 0
            if self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.FLAGGED,
                flag_value,
            ):
                changed_uids.append(message_uid)
            updates.append({"uid": message_uid, "flags": {"flagged": flagged}})
        if changed_uids:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, changed_uids
            )
        return {"updates": updates}

    def _toggle_messages_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_seen = bool(info.get_flags() & Camel.MessageFlags.SEEN)
            new_seen = not currently_seen
            flag_value = Camel.MessageFlags.SEEN if new_seen else 0
            if self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.SEEN,
                flag_value,
            ):
                changed_uids.append(message_uid)
            updates.append({"uid": message_uid, "flags": {"seen": new_seen}})
        if changed_uids:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, changed_uids
            )
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
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder.get_message_info(message_uid)
            if info is None:
                continue
            currently_flagged = bool(info.get_flags() & Camel.MessageFlags.FLAGGED)
            new_flagged = not currently_flagged
            flag_value = Camel.MessageFlags.FLAGGED if new_flagged else 0
            if self._apply_message_flags_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                Camel.MessageFlags.FLAGGED,
                flag_value,
            ):
                changed_uids.append(message_uid)
            updates.append({"uid": message_uid, "flags": {"flagged": new_flagged}})
        if changed_uids:
            self._persist_message_flag_changes_unlocked(
                account_uid, folder, changed_uids
            )
        return {"updates": updates}

    def _move_messages_to_trash_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        account = self.get_account(account_uid)
        if account.backend == "spool":
            return self._spool_trash_messages_unlocked(
                account_uid, folder_name, message_uids
            )

        store = self._get_store_unlocked(account_uid)
        folders = self._list_folders_unlocked(account_uid)
        trash_info = find_trash_folder(
            folders,
            trash_type=Camel.FolderInfoFlags.TYPE_TRASH,
            type_mask=Camel.FOLDER_TYPE_MASK,
        )
        if trash_info is None:
            raise ValueError("Trash folder not found for this account")
        trash_folder = store.get_folder_sync(trash_info["full_name"], 0, None)
        if trash_folder is None:
            raise ValueError("Trash folder not found for this account")

        return self._transfer_messages_unlocked(
            account_uid, folder_name, message_uids, trash_folder
        )

    def _spool_trash_messages_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        """Delete messages from an mbox spool inbox.

        Camel crashes when moving from CamelSpoolStore to VTrashFolder, so spool
        accounts remove messages via DELETED + expunge instead.
        """
        if not message_uids:
            return {"moved_uids": []}

        source_folder = self._open_folder_unlocked(account_uid, folder_name)
        deleted_mask = Camel.MessageFlags.DELETED
        for message_uid in message_uids:
            source_folder.set_message_flags(
                message_uid, deleted_mask, deleted_mask
            )
        source_folder.expunge_sync(None)
        source_folder.refresh_info_sync(None)

        moved_uids = list(message_uids)
        source_unread = source_folder.get_unread_message_count()
        source_total = source_folder.get_message_count()
        self._remove_messages_from_cache(
            account_uid, folder_name, moved_uids, source_unread, source_total
        )

        return {
            "moved_uids": moved_uids,
            "destination_uids": [],
            "source_folder": folder_name,
            "source_folder_unread": source_unread,
            "source_folder_total": source_total,
            "destination_folder": ".#evolution/Trash",
            "destination_folder_unread": -1,
            "destination_folder_total": -1,
        }

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

    @staticmethod
    def _transfer_uids_in_folder(
        folder: Camel.Folder, message_uids: list[str]
    ) -> list[str]:
        seen: set[str] = set()
        transfer_uids: list[str] = []
        for raw_uid in message_uids:
            uid = normalize_camel_uid(raw_uid)
            if uid is None or uid in seen:
                continue
            if folder.get_message_info(uid) is None:
                continue
            seen.add(uid)
            transfer_uids.append(uid)
        return transfer_uids

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

        transfer_uids = self._transfer_uids_in_folder(source_folder, message_uids)
        if not transfer_uids:
            return {"moved_uids": []}

        source_messages = self._message_dicts_for_uids_unlocked(
            source_folder, transfer_uids
        )

        destination_uids: list[str] = []
        for offset in range(0, len(transfer_uids), _TRANSFER_MESSAGE_BATCH_SIZE):
            batch = transfer_uids[offset : offset + _TRANSFER_MESSAGE_BATCH_SIZE]
            ok, transferred = source_folder.transfer_messages_to_sync(
                batch, destination_folder, True, None
            )
            if not ok:
                raise RuntimeError("Could not move messages")
            destination_uids.extend(camel_uid_list(transferred))

        self._commit_folder_transfer_unlocked(source_folder, destination_folder)

        # Camel returns destination UIDs; the UI and cache use source UIDs.
        moved_uids = list(transfer_uids)
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

    def _persist_message_flag_changes_unlocked(
        self,
        account_uid: str,
        folder: Camel.Folder,
        message_uids: list[str],
    ) -> None:
        if not message_uids:
            return
        store = self._get_store_unlocked(account_uid)
        self._persist_folder_flags_unlocked(store, folder, message_uids)

    @staticmethod
    def _persist_folder_flags_unlocked(
        store: Camel.Store,
        folder: Camel.Folder,
        message_uids: list[str],
    ) -> None:
        _persist_folder_flags(store, folder, message_uids)

    @staticmethod
    def _camel_uid_list(value: Any) -> list[str]:
        return camel_uid_list(value)

    @staticmethod
    def _commit_folder_transfer_unlocked(
        source_folder: Camel.Folder,
        destination_folder: Camel.Folder,
    ) -> None:
        """Push a folder transfer to the mail store (required for IMAP)."""
        if not source_folder.synchronize_sync(True, None):
            raise RuntimeError("Could not synchronize source folder after move")
        destination_folder.synchronize_sync(False, None)

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
    ) -> bool:
        return _apply_message_flags(
            folder,
            message_uid,
            mask,
            value,
            on_seen_changed=(
                lambda seen: self._update_cached_message_flags(
                    account_uid, folder_name, message_uid, seen=seen
                )
                if mask & Camel.MessageFlags.SEEN
                else None
            ),
            on_flagged_changed=(
                lambda flagged: self._update_cached_message_flags(
                    account_uid, folder_name, message_uid, flagged=flagged
                )
                if mask & Camel.MessageFlags.FLAGGED
                else None
            ),
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
            self._correspondent_indexes.pop(account_uid, None)

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        return guess_inbox_name(folders)
