# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later AND LicenseRef-MIT-EvolutionMCP
#
# EDS/Camel glue derived from EvolutionMCP (MIT) — see LICENSES/LicenseRef-MIT-EvolutionMCP.txt

"""EDS SourceRegistry + Camel session.

Mail I/O threading
------------------
Blocking Camel calls must run on the dedicated mail I/O thread
(``post.mail.io_thread``).  :class:`MailService` is the facade: public methods
either execute on the mail thread already or dispatch via
``get_mail_io_thread().run_sync`` / ``submit``.  UI code must not call
``run_sync`` from the GTK thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence, Callable

FolderIndexSource = Literal["memory", "disk_cache", "local", "server"]

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")
gi.require_version("Gio", "2.0")

from gi.repository import Camel, EDataServer, GLib, Gio

from . import folder_index_cache
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
from .camel_util import (
    camel_uid_to_api,
    camel_uid_list,
    folder_get_message_info,
    folder_get_uids,
    folder_search_uids,
    normalize_camel_uid,
)
from .message_flags import (
    apply_message_flags as _apply_message_flags,
    mark_message_seen as _mark_message_seen,
    persist_folder_flags as _persist_folder_flags,
)
from .local_delivery import all_recipients_local, can_deliver_locally, deliver_local_message
from .io_thread import get_mail_io_thread, is_mail_io_thread, run_on_mail_thread
from .send_errors import (
    MESSAGE_QUEUED,
    SYSTEM_MAIL_EXTERNAL_RECIPIENTS,
    SendError,
    SendQueued,
    is_compose_validation_error,
    user_send_error_message,
)
from .draft_queue import (
    QueuedDraft,
    count_queued_drafts,
    enqueue_draft,
    is_queued_draft_id,
    list_queued_drafts,
    load_queued_draft_attachments,
    remove_queued_draft,
)
from .operation_queue import (
    OperationType,
    QueuedOperation,
    count_queued_operations,
    enqueue_operation,
    list_queued_operations,
    remove_queued_operation,
)
from .send_queue import (
    QueuedOutboundMessage,
    enqueue_outbound_message,
    is_network_unavailable_error,
    is_outbound_ready_to_send,
    is_queueable_network_error,
    list_queued_outbound_messages,
    load_queued_attachments,
    load_queued_outbound_message,
    remove_queued_outbound_message,
)
from post.preferences import get_show_evolution_local
from .auth import PasswordPromptCallback, authenticate_service_sync, ensure_goa_credentials
from .compose import (
    ComposeAttachment,
    addresses_to_internet_address,
    build_draft_mime_message,
    build_plain_mime_message,
    new_outbound_mime_identifiers,
    normalize_email,
    normalize_references_header,
)
from .correspondents import Correspondent, collect_correspondents
from .folders import (
    find_folder_by_type,
    find_trash_folder,
    folder_can_contain_messages,
    folder_name_from_uri,
    guess_inbox_name,
    is_post_outbox_folder,
    is_virtual_folder,
    validate_folder_display_name,
)
from post.preferences import get_account_user_online
from .offline_settings import apply_offline_settings_to_store, apply_offline_sync_to_folder
from .offline_sync import OfflineBodySyncCoordinator, OfflineSyncProgress
from .search import (
    MessageSearchQuery,
    SearchCompleteCallback,
    SearchFilterProgress,
    SearchMatchCallback,
    SearchProgressCallback,
    SearchScanCursor,
    annotate_search_match,
    filter_messages_by_query,
    query_requires_body_scan,
)
from .search_debug import search_trace, search_trace_timer

log = logging.getLogger(__name__)

# Limit autocomplete index size; messages are processed newest-first.
_MAX_CORRESPONDENTS = 500

# EDS also lists RSS feeds, search folders, etc. as "Mail Account" sources.
_SKIP_BACKENDS = frozenset({"rss", "vfolder"})
DEFAULT_MESSAGE_PAGE_SIZE = 50
_SEND_TIMEOUT_SECONDS = 30
_DRAFT_TIMEOUT_SECONDS = 30
# Stay below Camel IMAPx MAX_UIDSET_ITEMS (100) to avoid spurious uidset warnings
# in evolution-data-server 3.56 when batching UID MOVE/COPY commands.
_TRANSFER_MESSAGE_BATCH_SIZE = 50


def _run_on_gtk_thread(callback: Callable[[], None]) -> bool:
    callback()
    return False


class MessageUnavailableReason:
    VANISHED = "vanished"
    NOT_CACHED_OFFLINE = "not_cached_offline"


class MessageNotAvailableError(LookupError):
    """Raised when Camel reports a listed UID is no longer fetchable."""

    def __init__(
        self,
        message_uid: str,
        folder_name: str | None = None,
        *,
        reason: str = MessageUnavailableReason.VANISHED,
    ) -> None:
        self.message_uid = message_uid
        self.folder_name = folder_name
        self.reason = reason
        super().__init__(message_uid)

    def user_message(self) -> str:
        if self.reason == MessageUnavailableReason.NOT_CACHED_OFFLINE:
            return (
                "This message isn't available offline yet. "
                "Connect to download it."
            )
        return "This message is no longer available."


@dataclass
class _FolderMessageIndex:
    messages: list[dict]
    unread: int
    total: int


def _folder_index_is_cacheable(index: _FolderMessageIndex) -> bool:
    if index.total <= 0:
        return True
    return len(index.messages) >= index.total


def _read_unflagged_uids(index: _FolderMessageIndex) -> list[str]:
    return [
        message["uid"]
        for message in index.messages
        if message.get("uid") and message_is_read_unflagged(message)
    ]


# Camel providers that authenticate via session OAuth (not IMAP password).
_OAUTH_SERVICE_MECHANISMS = frozenset({"XOAUTH2", "Microsoft365"})


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
        """Password auth for IMAP; OAuth for XOAUTH2 / Microsoft 365 providers."""
        if mechanism in _OAUTH_SERVICE_MECHANISMS:
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
    """Facade around EDS + Camel; blocking I/O is routed to the mail I/O thread."""

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
    _outbound_sends_in_progress: int = field(default=0, init=False)
    _outbound_sends_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )
    _active_outbound_deliveries: set[str] = field(default_factory=set, init=False)
    _flushing_operation_queue: bool = field(default=False, init=False)
    _flushing_draft_queue: bool = field(default=False, init=False)
    _offline_sync: OfflineBodySyncCoordinator | None = field(
        default=None, init=False, repr=False
    )
    _mail_io_callbacks_registered: bool = field(default=False, init=False, repr=False)
    _folder_search_cancellable: Gio.Cancellable | None = field(
        default=None, init=False, repr=False
    )
    _folder_search_state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _sync_setup_cancel: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )
    _folder_list_cancellable: Gio.Cancellable | None = field(
        default=None, init=False, repr=False
    )
    _folder_list_state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @property
    def offline_sync(self) -> OfflineBodySyncCoordinator:
        if self._offline_sync is None:
            self._offline_sync = OfflineBodySyncCoordinator(self)
        return self._offline_sync

    def _ensure_mail_io_callbacks(self) -> None:
        if self._mail_io_callbacks_registered:
            return
        get_mail_io_thread().set_background_preempt_callbacks(
            self._preempt_background_work,
            self.schedule_offline_body_sync,
        )
        self._mail_io_callbacks_registered = True

    def _preempt_background_work(self) -> None:
        self.cancel_folder_search()
        self.cancel_folder_list()
        self.offline_sync.cancel_all()
        if self._sync_setup_cancel is not None:
            self._sync_setup_cancel()

    def cancel_folder_list(self) -> None:
        with self._folder_list_state_lock:
            cancellable = self._folder_list_cancellable
            self._folder_list_cancellable = None
        if cancellable is not None:
            search_trace("folder_list_cancel")
            cancellable.cancel()

    def _register_folder_list_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            previous = self._folder_list_cancellable
            self._folder_list_cancellable = cancellable
        if previous is not None and previous is not cancellable:
            previous.cancel()

    def _unregister_folder_list_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            if self._folder_list_cancellable is cancellable:
                self._folder_list_cancellable = None

    def set_sync_setup_cancel_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        self._sync_setup_cancel = callback

    def cancel_folder_search(self) -> None:
        """Abort an in-flight Camel folder search so interactive reads can proceed."""
        with self._folder_search_state_lock:
            cancellable = self._folder_search_cancellable
            self._folder_search_cancellable = None
        search_trace(
            "search_cancel",
            had_cancellable=cancellable is not None,
            was_cancelled=cancellable.is_cancelled() if cancellable is not None else None,
        )
        if cancellable is not None:
            cancellable.cancel()

    def _cancel_folder_search_unlocked(self) -> None:
        with self._folder_search_state_lock:
            cancellable = self._folder_search_cancellable
            self._folder_search_cancellable = None
        if cancellable is not None:
            cancellable.cancel()

    def _begin_folder_search_unlocked(self) -> Gio.Cancellable:
        self._cancel_folder_search_unlocked()
        cancellable = Gio.Cancellable()
        with self._folder_search_state_lock:
            self._folder_search_cancellable = cancellable
        return cancellable

    def _end_folder_search_unlocked(self, cancellable: Gio.Cancellable) -> None:
        with self._folder_search_state_lock:
            if self._folder_search_cancellable is cancellable:
                self._folder_search_cancellable = None

    def is_network_available(self) -> bool:
        return self._network_available

    def schedule_offline_body_sync(self, account_uid: str | None = None) -> None:
        if account_uid is None:
            self.offline_sync.schedule_all_accounts()
        else:
            self.offline_sync.schedule_account(account_uid)

    def cancel_offline_body_sync(self, account_uid: str) -> None:
        self.offline_sync.cancel_account(account_uid)

    def refresh_offline_settings(self, account_uid: str) -> None:
        """Re-apply offline Camel settings after preference changes."""
        if is_mail_io_thread():
            self._refresh_offline_settings_unlocked(account_uid)
        else:
            get_mail_io_thread().submit(
                self._refresh_offline_settings_unlocked, account_uid
            )

    def _refresh_offline_settings_unlocked(self, account_uid: str) -> None:
        try:
            store = self._get_store_unlocked(account_uid)
        except Exception:
            log.debug(
                "Cannot refresh offline settings for %s",
                account_uid,
                exc_info=True,
            )
            return
        apply_offline_settings_to_store(store, account_uid)
        self.schedule_offline_body_sync(account_uid)

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

    def outbound_sends_pending(self) -> bool:
        with self._outbound_sends_cond:
            return self._outbound_sends_in_progress > 0

    def _begin_outbound_send(self) -> None:
        with self._outbound_sends_cond:
            self._outbound_sends_in_progress += 1

    def _end_outbound_send(self) -> None:
        with self._outbound_sends_cond:
            if self._outbound_sends_in_progress > 0:
                self._outbound_sends_in_progress -= 1
            if self._outbound_sends_in_progress == 0:
                self._outbound_sends_cond.notify_all()

    def _reset_outbound_send_counter_after_timeout(self) -> None:
        with self._outbound_sends_cond:
            if self._outbound_sends_in_progress <= 0:
                return
            log.warning(
                "Resetting stuck outbound send counter (%d) after wait timeout",
                self._outbound_sends_in_progress,
            )
            self._outbound_sends_in_progress = 0
            self._outbound_sends_cond.notify_all()

    def claim_outbound_delivery(self, queue_id: str) -> None:
        """Reserve an outbox item for an active compose delivery worker."""
        with self._outbound_sends_cond:
            self._active_outbound_deliveries.add(queue_id)

    def release_outbound_delivery(self, queue_id: str) -> None:
        with self._outbound_sends_cond:
            self._active_outbound_deliveries.discard(queue_id)

    def _is_outbound_delivery_claimed(self, queue_id: str) -> bool:
        with self._outbound_sends_cond:
            return queue_id in self._active_outbound_deliveries

    def begin_outbound_send(self) -> None:
        self._begin_outbound_send()

    def end_outbound_send(self) -> None:
        self._end_outbound_send()

    def wait_for_outbound_sends(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._outbound_sends_cond:
            while self._outbound_sends_in_progress > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "Timed out waiting for %d outbound send(s)",
                        self._outbound_sends_in_progress,
                    )
                    return False
                self._outbound_sends_cond.wait(timeout=remaining)
        return True

    def when_outbound_sends_complete(
        self,
        callback: Callable[[], None],
        *,
        timeout: float = 120.0,
    ) -> None:
        """Run callback on the GTK thread after outbound sends finish.

        Waits on a worker thread so the UI main loop is not blocked.
        """

        def worker() -> None:
            completed = self.wait_for_outbound_sends(timeout=timeout)
            if not completed:
                self._reset_outbound_send_counter_after_timeout()
            GLib.idle_add(_run_on_gtk_thread, callback)

        threading.Thread(target=worker, daemon=True, name="post-send-wait").start()

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
        """Best-effort store flush before exit; never wait for offline body download."""
        self.wait_for_outbound_sends()
        self.cancel_folder_search()
        offline_sync_active = self.offline_sync.is_active()
        self.offline_sync.cancel_all()
        self.wait_for_pending_mail_ops(timeout=2.0 if offline_sync_active else 1.0)
        # Never block GTK exit behind a long in-flight search or folder scan.
        get_mail_io_thread().submit_background(self._flush_stores_on_shutdown)

    def _flush_stores_on_shutdown(self) -> None:
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
        service = cls(registry=registry)
        service._ensure_mail_io_callbacks()
        return service

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback
        if isinstance(self._session, MailSession):
            self._session.set_password_prompt(callback)

    def set_network_available(self, available: bool) -> None:
        """Update Camel session/store online state from Gio.NetworkMonitor."""
        run_on_mail_thread(self._set_network_available_unlocked, available)

    def _set_network_available_unlocked(self, available: bool) -> None:
        stores_to_sync: list[tuple[str, Camel.Store, bool]] = []
        with self._lock:
            if self._network_available == available:
                return
            self._network_available = available
            if self._session is not None:
                self._session.set_online(available)
            for account_uid, store in self._stores.items():
                if isinstance(store, Camel.OfflineStore):
                    effective = available and get_account_user_online(account_uid)
                    stores_to_sync.append((account_uid, store, effective))
        for account_uid, store, effective in stores_to_sync:
            try:
                store.set_online_sync(effective, None)
            except GLib.Error:
                log.debug(
                    "Could not set store offline=%s",
                    not effective,
                    exc_info=True,
                )
        if available:
            self.offline_sync.schedule_all_accounts()

    def go_online_sync(self) -> None:
        """Bring offline stores back online and drop cached folder indexes."""
        run_on_mail_thread(self._go_online_sync_unlocked)

    def set_account_user_online(self, account_uid: str, online: bool) -> None:
        """Persist and apply per-account user online/offline state."""
        from post.preferences import set_account_user_online as save_pref

        save_pref(account_uid, online)
        run_on_mail_thread(self._apply_account_user_online_unlocked, account_uid)

    def _apply_account_user_online_unlocked(self, account_uid: str) -> None:
        online = get_account_user_online(account_uid)
        store: Camel.Store | None = None
        effective = False
        with self._lock:
            candidate = self._stores.get(account_uid)
            if isinstance(candidate, Camel.OfflineStore):
                store = candidate
                effective = self._network_available and online
        if store is not None:
            try:
                store.set_online_sync(effective, None)
            except GLib.Error:
                log.debug(
                    "Could not set account %s online=%s",
                    account_uid,
                    effective,
                    exc_info=True,
                )
        if online:
            self.schedule_offline_body_sync(account_uid)
        else:
            self.cancel_offline_body_sync(account_uid)

    def _go_online_sync_unlocked(self) -> None:
        stores_to_online: list[Camel.Store] = []
        with self._lock:
            self._network_available = True
            if self._session is not None:
                self._session.set_online(True)
            for account_uid, store in self._stores.items():
                if not get_account_user_online(account_uid):
                    continue
                if isinstance(store, Camel.OfflineStore):
                    stores_to_online.append(store)
            self._folder_indexes.clear()
        for store in stores_to_online:
            try:
                store.set_online_sync(True, None)
            except GLib.Error:
                log.exception("Failed to bring mail store online")
        self.offline_sync.schedule_all_accounts()

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

    def _call_without_service_lock(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` with ``_lock`` fully released (all re-entrant levels).

        Camel connect / set_online can prompt for a password via the GTK main
        loop. Holding ``_lock`` across that deadlocks when the UI also needs the
        lock (for example ``get_account`` during startup).
        """
        depth = self._lock._recursion_count()
        if depth <= 0:
            return func(*args, **kwargs)
        for _ in range(depth):
            self._lock.release()
        try:
            return func(*args, **kwargs)
        finally:
            for _ in range(depth):
                self._lock.acquire()

    def _ensure_session(self) -> Camel.Session:
        if self._session is not None:
            self._session.set_online(self._network_available)
            return self._session

        user_data = os.path.expanduser("~/.local/share/evolution")
        user_cache = os.path.expanduser("~/.cache/evolution")
        if not is_mail_io_thread():
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

    def get_store_for_sync_if_ready(self, account_uid: str) -> Camel.Store | None:
        """Return a connected store without opening a new connection."""
        with self._lock:
            store = self._stores.get(account_uid)
        if store is None:
            return None
        if (
            store.get_connection_status()
            != Camel.ServiceConnectionStatus.CONNECTED
        ):
            return None
        return store

    def seed_folder_index(
        self,
        account_uid: str,
        folder_name: str,
        messages: list[dict],
        unread: int,
        total: int,
    ) -> None:
        """Install a folder index from disk cache without touching Camel."""
        with self._lock:
            self._folder_indexes[(account_uid, folder_name)] = _FolderMessageIndex(
                messages=list(messages),
                unread=unread,
                total=total,
            )

    def get_folder_index_snapshot(
        self,
        account_uid: str,
        folder_name: str,
    ) -> tuple[list[dict], int, int] | None:
        """Return the in-memory folder index when already loaded."""
        return self._get_folder_index_snapshot_unlocked(
            account_uid, folder_name
        )

    def _get_folder_index_snapshot_unlocked(
        self,
        account_uid: str,
        folder_name: str,
    ) -> tuple[list[dict], int, int] | None:
        with self._lock:
            index = self._folder_indexes.get((account_uid, folder_name))
        if index is None:
            return None
        return list(index.messages), index.unread, index.total

    def invalidate_folder_index(self, account_uid: str, folder_name: str) -> None:
        with self._lock:
            self._invalidate_folder_index(account_uid, folder_name)

    def invalidate_correspondent_index(self, account_uid: str) -> None:
        with self._lock:
            self._correspondent_indexes.pop(account_uid, None)

    def get_inbox_folder_name(self, account_uid: str) -> str | None:
        """Return INBOX folder name, using the cached folder tree when available."""
        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)
        if cached is not None:
            return guess_inbox_name(cached)
        return guess_inbox_name(self.list_folders(account_uid))

    def get_inbox_folder_name_cached(self, account_uid: str) -> str | None:
        """Return INBOX from the folder-tree cache, or None if not loaded yet.

        Do not guess ``INBOX``: Microsoft 365 and other backends often use a
        different Camel folder name, and a hardcoded fallback causes sync-watch
        errors (#153).
        """
        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)
        if cached is not None:
            return guess_inbox_name(cached)
        return None

    def prepare_account_credentials(self, account_uid: str) -> None:
        """Refresh GOA tokens before mail I/O (may show account sign-in UI)."""
        source = self.registry.ref_source(account_uid)
        if source is None:
            return
        if source.has_extension("GNOME Online Accounts"):
            ensure_goa_credentials(self.registry, source, None)

    def _prepare_account_credentials_unlocked(
        self,
        account_uid: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        source = self.registry.ref_source(account_uid)
        if source is None:
            return
        if source.has_extension("GNOME Online Accounts"):
            ensure_goa_credentials(self.registry, source, cancellable)

    def _get_store_unlocked(
        self,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> Camel.Store:
        if account_uid in self._stores:
            store = self._stores[account_uid]
            if store.get_connection_status() == Camel.ServiceConnectionStatus.CONNECTED:
                self._call_without_service_lock(
                    self._sync_store_online_state_unlocked,
                    store,
                    account_uid,
                    cancellable=cancellable,
                )
                self._configure_store_settings_unlocked(store, account_uid)
                return store
            del self._stores[account_uid]

        source = self.registry.ref_source(account_uid)
        if source is None:
            raise ValueError(f"Unknown mail account: {account_uid}")

        self._call_without_service_lock(
            self._prepare_account_credentials_unlocked,
            account_uid,
            cancellable,
        )
        session = self._ensure_session()
        mail_ext = source.get_extension("Mail Account")
        service = session.add_service(
            account_uid, mail_ext.get_backend_name(), Camel.ProviderType.STORE
        )
        if service is None:
            raise RuntimeError(f"Could not create mail store for {account_uid}")

        source.camel_configure_service(service)
        store = service
        self._stores[account_uid] = store

        self._call_without_service_lock(
            self._sync_store_online_state_unlocked,
            store,
            account_uid,
            cancellable=cancellable,
        )

        self._configure_store_settings_unlocked(store, account_uid)
        return store

    def _sync_store_online_state_unlocked(
        self,
        store: Camel.Store,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Connect / set online. Must not run while ``_lock`` is held."""
        effective = self._network_available and get_account_user_online(account_uid)
        if isinstance(store, Camel.OfflineStore):
            store.set_online_sync(effective, cancellable)
        elif effective:
            store.connect_sync(cancellable)

    @staticmethod
    def _configure_store_settings_unlocked(
        store: Camel.Store, account_uid: str
    ) -> None:
        apply_offline_settings_to_store(store, account_uid)

    @staticmethod
    def _apply_store_settings_to_transport(
        store: Camel.Store, transport: Camel.Transport
    ) -> None:
        """Copy mailbox settings from the account store onto the transport.

        GOA Microsoft 365 transport ESources are stubs (backend name only).
        Without the store's ``user`` / OAuth settings, Camel probes Graph
        ``mailFolders`` and fails with ResourceNotFound (#151).
        """
        try:
            settings = store.get_property("settings")
        except Exception:
            log.debug(
                "Could not read store settings for transport",
                exc_info=True,
            )
            return
        if settings is None:
            return
        transport.set_settings(settings)

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

        self._call_without_service_lock(
            self._prepare_account_credentials_unlocked, account_uid
        )
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
        self._transports[transport_uid] = transport

        if (backend or "").lower() == "microsoft365":
            store = self._get_store_unlocked(account_uid, cancellable=cancellable)
            self._apply_store_settings_to_transport(store, transport)

        def _connect_transport() -> None:
            if hasattr(Camel, "OfflineTransport") and isinstance(
                transport, Camel.OfflineTransport
            ):
                transport.set_online_sync(True, cancellable)
            else:
                transport.connect_sync(cancellable)

        self._call_without_service_lock(_connect_transport)
        return transport

    def flush_send_queue(self, *, force: bool = False) -> int:
        """Try to send queued outbox messages. Returns count sent."""
        if is_mail_io_thread():
            return self._flush_send_queue_unlocked(force=force)
        return get_mail_io_thread().run_sync(
            self._flush_send_queue_unlocked, force=force
        )

    def flush_operation_queue(self) -> int:
        """Apply queued mail mutations after reconnect. Returns count flushed."""
        if is_mail_io_thread():
            return self._flush_operation_queue_unlocked()
        return get_mail_io_thread().run_sync(self._flush_operation_queue_unlocked)

    def count_queued_operations(self) -> int:
        return count_queued_operations()

    def count_queued_drafts(self) -> int:
        return count_queued_drafts()

    def flush_draft_queue(self) -> int:
        """Append queued drafts to Drafts folders after reconnect."""
        if is_mail_io_thread():
            return self._flush_draft_queue_unlocked()
        return get_mail_io_thread().run_sync(self._flush_draft_queue_unlocked)

    def send_message(
        self,
        account_uid: str,
        *,
        to: list[str],
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str,
        body: str,
        body_html: str | None = None,
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
                body_html=body_html,
                in_reply_to=in_reply_to,
                references=references,
                attachments=attachments,
                from_queue=False,
            )

    def deliver_outbound_queue_item(self, queue_id: str) -> None:
        """SMTP-deliver a persisted outbox item and remove it on success."""
        if is_mail_io_thread():
            self._deliver_outbound_queue_item_impl(queue_id)
            return
        get_mail_io_thread().run_sync(self._deliver_outbound_queue_item_impl, queue_id)

    def _deliver_outbound_queue_item_impl(self, queue_id: str) -> None:
        with self._lock:
            queued = load_queued_outbound_message(queue_id)
            attachments = load_queued_attachments(queue_id, queued)
        self._send_message_unlocked(
            queued.account_uid,
            to=queued.to,
            cc=queued.cc,
            bcc=queued.bcc,
            subject=queued.subject,
            body=queued.body,
            body_html=queued.body_html,
            in_reply_to=queued.in_reply_to,
            references=queued.references,
            attachments=attachments,
            from_queue=True,
            queue_id=queue_id,
        )

    def send_outbound_queue_item(self, queue_id: str) -> None:
        """Send a persisted outbox item (flush / retry path)."""
        self._begin_outbound_send()
        try:
            self.deliver_outbound_queue_item(queue_id)
        finally:
            self._end_outbound_send()

    def _send_message_unlocked(
        self,
        account_uid: str,
        *,
        to: list[str],
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,
        body_html: str | None = None,
        in_reply_to: str | None,
        references: str | None,
        attachments: Sequence[ComposeAttachment] | None = None,
        from_queue: bool = False,
        queue_id: str | None = None,
    ) -> None:
        send_start = time.monotonic()
        log.debug(
            "Sending message account=%s to=%d cc=%d bcc=%d subject=%r",
            account_uid,
            len(to),
            len(cc or []),
            len(bcc or []),
            subject or "",
        )
        log.debug("Recipients to=%s cc=%s bcc=%s", to, cc, bcc)
        if queue_id:
            log.debug("Sending outbox item %s", queue_id)

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
            "body_html": body_html,
            "in_reply_to": in_reply_to,
            "references": references,
            "attachments": attachments,
        }
        outbound_ids = new_outbound_mime_identifiers(from_address)

        message = build_plain_mime_message(
            **compose_kwargs,
            include_bcc_header=False,
            message_id=outbound_ids.message_id,
            date=outbound_ids.date,
        )
        sent_message = build_plain_mime_message(
            **compose_kwargs,
            message_id=outbound_ids.message_id,
            date=outbound_ids.date,
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
                    if queue_id:
                        remove_queued_outbound_message(queue_id)
                    log.debug(
                        "Send finished account=%s in %.2fs (local delivery)",
                        account_uid,
                        time.monotonic() - send_start,
                    )
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
        smtp_start = time.monotonic()
        transport_uid = ""
        try:
            with self._lock:
                transport = self._get_transport_unlocked(account_uid, cancellable)
            transport_uid = account.transport_uid or ""
            ok, _user_stop = transport.send_to_sync(
                message, sender, recipients, cancellable
            )
        except GLib.Error as exc:
            log.debug(
                "SMTP send finished in %.2fs cancelled=%s",
                time.monotonic() - smtp_start,
                cancellable.is_cancelled(),
            )
            network_exc: BaseException = (
                TimeoutError() if cancellable.is_cancelled() else exc
            )
            if is_queueable_network_error(network_exc):
                if queue_id is None and not from_queue:
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
                log.warning(
                    "Send timed out after %ds account=%s",
                    _SEND_TIMEOUT_SECONDS,
                    account_uid,
                )
                raise SendError(user_send_error_message(TimeoutError())) from exc
            log.warning(
                "Send failed account=%s in %.2fs: %s",
                account_uid,
                time.monotonic() - send_start,
                exc.message,
            )
            raise SendError(user_send_error_message(exc)) from exc
        finally:
            timer.cancel()
            if transport is not None:
                try:
                    transport.disconnect_sync(True, cancellable)
                except Exception:
                    log.debug("Failed to disconnect transport after send", exc_info=True)
            if transport_uid:
                with self._lock:
                    self._transports.pop(transport_uid, None)

        log.debug("SMTP send finished in %.2fs ok=%s", time.monotonic() - smtp_start, ok)

        if not ok:
            log.warning(
                "Send failed account=%s in %.2fs: transport returned false",
                account_uid,
                time.monotonic() - send_start,
            )
            raise SendError(user_send_error_message(RuntimeError("Could not send message")))

        self._append_sent_copy_and_finish_queue_item(
            account_uid,
            sent_message,
            queue_id,
            send_start=send_start,
            from_queue=from_queue,
        )

    def _append_sent_copy_and_finish_queue_item(
        self,
        account_uid: str,
        sent_message: Camel.MimeMessage,
        queue_id: str | None,
        *,
        send_start: float,
        from_queue: bool = False,
    ) -> None:
        self._append_to_sent_folder_unlocked(account_uid, sent_message)
        if queue_id:
            remove_queued_outbound_message(queue_id)
        log.debug(
            "Send finished account=%s in %.2fs",
            account_uid,
            time.monotonic() - send_start,
        )

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

    def _flush_send_queue_unlocked(self, *, force: bool = False) -> int:
        sent = 0
        for queue_id, queued in list_queued_outbound_messages():
            if not force and not is_outbound_ready_to_send(queued):
                continue
            if self._is_outbound_delivery_claimed(queue_id):
                continue
            self._begin_outbound_send()
            try:
                try:
                    self._send_message_unlocked(
                        queued.account_uid,
                        to=queued.to,
                        cc=queued.cc,
                        bcc=queued.bcc,
                        subject=queued.subject,
                        body=queued.body,
                        body_html=queued.body_html,
                        in_reply_to=queued.in_reply_to,
                        references=queued.references,
                        attachments=load_queued_attachments(queue_id, queued),
                        from_queue=True,
                        queue_id=queue_id,
                    )
                except SendQueued:
                    break
                except SendError as exc:
                    if is_compose_validation_error(exc):
                        remove_queued_outbound_message(queue_id)
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
                    sent += 1
            finally:
                self._end_outbound_send()
        return sent

    def _flush_operation_queue_unlocked(self) -> int:
        flushed = 0
        self._flushing_operation_queue = True
        try:
            for queue_id, operation in list_queued_operations():
                try:
                    self._execute_queued_operation_unlocked(operation)
                except Exception:
                    log.exception(
                        "Failed to flush queued operation %s (%s)",
                        queue_id,
                        operation.op_type,
                    )
                    break
                else:
                    remove_queued_operation(queue_id)
                    flushed += 1
        finally:
            self._flushing_operation_queue = False
        return flushed

    def _flush_draft_queue_unlocked(self) -> int:
        flushed = 0
        self._flushing_draft_queue = True
        try:
            for queue_id, queued in list_queued_drafts():
                try:
                    attachments = load_queued_draft_attachments(queue_id, queued)
                    self._save_draft_unlocked(
                        queued.account_uid,
                        to=queued.to,
                        cc=queued.cc,
                        bcc=queued.bcc,
                        subject=queued.subject,
                        body=queued.body,
                        body_html=queued.body_html,
                        in_reply_to=queued.in_reply_to,
                        references=queued.references,
                        existing_uid=queued.existing_uid,
                        drafts_folder_name=queued.drafts_folder_name,
                        attachments=attachments,
                    )
                except Exception:
                    log.exception(
                        "Failed to flush queued draft %s for account %s",
                        queue_id,
                        queued.account_uid,
                    )
                    break
                else:
                    remove_queued_draft(queue_id)
                    flushed += 1
        finally:
            self._flushing_draft_queue = False
        return flushed

    def _execute_queued_operation_unlocked(self, operation: QueuedOperation) -> None:
        if operation.op_type == "move_to_trash":
            self._move_messages_to_trash_unlocked(
                operation.account_uid,
                operation.folder_name,
                list(operation.message_uids),
            )
            return
        if operation.op_type == "archive":
            self._archive_messages_unlocked(
                operation.account_uid,
                operation.folder_name,
                list(operation.message_uids),
            )
            return
        if operation.op_type == "move_to_folder":
            if not operation.destination_folder:
                raise ValueError("Queued move is missing destination folder")
            self._move_messages_unlocked(
                operation.account_uid,
                operation.folder_name,
                operation.destination_folder,
                list(operation.message_uids),
            )
            return
        if operation.op_type == "set_seen":
            if operation.seen is None:
                raise ValueError("Queued seen update is missing seen flag")
            self._set_messages_seen_unlocked(
                operation.account_uid,
                operation.folder_name,
                list(operation.message_uids),
                seen=operation.seen,
            )
            return
        if operation.op_type == "set_flagged":
            if operation.flagged is None:
                raise ValueError("Queued flagged update is missing flagged flag")
            self._set_messages_flagged_unlocked(
                operation.account_uid,
                operation.folder_name,
                list(operation.message_uids),
                flagged=operation.flagged,
            )
            return
        raise ValueError(f"Unknown queued operation: {operation.op_type}")

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

        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)
        if cached is not None:
            sent_info = find_folder_by_type(
                cached,
                Camel.FolderInfoFlags.TYPE_OUTBOX,
                type_mask=Camel.FOLDER_TYPE_MASK,
                name_fallbacks=frozenset({"sent", "sent mail", "sent messages"}),
            )
            if sent_info is not None:
                return sent_info.get("full_name")

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

        cancellable = Gio.Cancellable()
        timer = threading.Timer(_SEND_TIMEOUT_SECONDS, cancellable.cancel)
        timer.start()
        append_start = time.monotonic()
        try:
            ok, _uid = folder.append_message_sync(message, None, cancellable)
        except GLib.Error as exc:
            if cancellable.is_cancelled():
                log.warning(
                    "Sent folder append timed out after %ds folder=%r",
                    _SEND_TIMEOUT_SECONDS,
                    folder_name,
                )
            else:
                log.warning(
                    "Failed to save a copy to Sent folder %r: %s",
                    folder_name,
                    exc.message,
                )
            return
        finally:
            timer.cancel()

        if not ok:
            log.warning("Could not append message to Sent folder %r", folder_name)
            return

        log.debug(
            "Sent folder append finished in %.2fs folder=%r",
            time.monotonic() - append_start,
            folder_name,
        )

        try:
            folder.refresh_info_sync(None)
        except GLib.Error:
            log.debug("Failed to refresh Sent folder after append", exc_info=True)
        self._invalidate_folder_index(account_uid, folder_name)

    def _drafts_folder_name_unlocked(
        self,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> str | None:
        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)
        folders = (
            cached
            if cached is not None
            else self._list_folders_unlocked(account_uid, cancellable=cancellable)
        )
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
        folder.set_message_flags(camel_uid_to_api(message_uid), deleted, deleted)
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
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> str | None:
        folder = self._open_folder_unlocked(
            account_uid, folder_name, cancellable=cancellable
        )
        if folder is None:
            raise RuntimeError(f"Drafts folder {folder_name!r} is not available")

        info = self._draft_message_info()
        try:
            result = folder.append_message_sync(message, info, cancellable)
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
            folder.refresh_info_sync(cancellable)
        except GLib.Error:
            log.debug("Failed to refresh Drafts folder after append", exc_info=True)
        self._invalidate_folder_index(account_uid, folder_name)

        if appended_uid:
            return str(appended_uid)
        uids = folder_get_uids(folder)
        if uids:
            return str(uids[-1])
        return None

    def _queue_draft_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        to: list[str] | None,
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str,
        body: str,
        body_html: str | None,
        in_reply_to: str | None,
        references: str | None,
        existing_uid: str | None,
        attachments: Sequence[ComposeAttachment] | None,
        queue_id: str | None = None,
    ) -> tuple[str, str]:
        replace_uid: str | None = None
        if existing_uid and is_queued_draft_id(existing_uid):
            remove_queued_draft(existing_uid)
            queue_id = existing_uid
        elif existing_uid:
            replace_uid = existing_uid

        queue_id = enqueue_draft(
            QueuedDraft(
                account_uid=account_uid,
                drafts_folder_name=folder_name,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                body_html=body_html,
                in_reply_to=in_reply_to,
                references=references,
                existing_uid=replace_uid,
            ),
            attachment_payloads=attachments,
            queue_id=queue_id,
        )
        return folder_name, queue_id

    def save_draft(
        self,
        account_uid: str,
        *,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str,
        body: str,
        body_html: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        existing_uid: str | None = None,
        drafts_folder_name: str | None = None,
        attachments: Sequence[ComposeAttachment] | None = None,
        cancellable: Gio.Cancellable | None = None,
    ) -> tuple[str, str]:
        """Save or update a draft. Returns (drafts_folder_name, message_uid)."""
        return run_on_mail_thread(
            self._save_draft_unlocked,
            account_uid,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            existing_uid=existing_uid,
            drafts_folder_name=drafts_folder_name,
            attachments=attachments,
            cancellable=cancellable,
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
        body_html: str | None,
        in_reply_to: str | None,
        references: str | None,
        existing_uid: str | None,
        drafts_folder_name: str | None,
        attachments: Sequence[ComposeAttachment] | None = None,
        cancellable: Gio.Cancellable | None = None,
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
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments,
        )

        own_cancellable = cancellable is None
        if cancellable is None:
            cancellable = Gio.Cancellable()
        timer = threading.Timer(_DRAFT_TIMEOUT_SECONDS, cancellable.cancel)
        timer.start()

        def _queue_local() -> tuple[str, str]:
            folder = drafts_folder_name
            if not folder:
                with self._lock:
                    cached_tree = self._folder_tree_cache.get(account_uid)
                if cached_tree:
                    drafts_info = find_folder_by_type(
                        cached_tree,
                        Camel.FolderInfoFlags.TYPE_DRAFTS,
                        type_mask=Camel.FOLDER_TYPE_MASK,
                        name_fallbacks=frozenset({"drafts", "draft"}),
                    )
                    if drafts_info is not None:
                        folder = drafts_info.get("full_name")
            if not folder:
                folder = "Drafts"
            return self._queue_draft_unlocked(
                account_uid,
                folder,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                body_html=body_html,
                in_reply_to=in_reply_to,
                references=references,
                existing_uid=existing_uid,
                attachments=attachments,
            )

        try:
            folder_name = drafts_folder_name or self._drafts_folder_name_unlocked(
                account_uid, cancellable=cancellable
            )
            if not folder_name:
                if cancellable.is_cancelled():
                    if self._flushing_draft_queue:
                        raise RuntimeError("Draft save timed out")
                    return _queue_local()
                raise RuntimeError("No Drafts folder is configured for this account")

            if not self._network_available and not self._flushing_draft_queue:
                return self._queue_draft_unlocked(
                    account_uid,
                    folder_name,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    body=body,
                    body_html=body_html,
                    in_reply_to=in_reply_to,
                    references=references,
                    existing_uid=existing_uid,
                    attachments=attachments,
                )

            if existing_uid:
                if is_queued_draft_id(existing_uid):
                    remove_queued_draft(existing_uid)
                else:
                    self._delete_message_unlocked(
                        account_uid, folder_name, existing_uid
                    )

            try:
                appended_uid = self._append_draft_unlocked(
                    account_uid,
                    folder_name,
                    message,
                    cancellable=cancellable,
                )
            except RuntimeError as exc:
                if self._flushing_draft_queue:
                    raise
                cause = exc.__cause__
                offline = is_network_unavailable_error(exc) or (
                    isinstance(cause, BaseException)
                    and is_network_unavailable_error(cause)
                )
                cancelled = cancellable.is_cancelled()
                if (
                    offline
                    or cancelled
                    or "working online" in str(exc).lower()
                    or "timed out" in str(exc).lower()
                ):
                    return self._queue_draft_unlocked(
                        account_uid,
                        folder_name,
                        to=to,
                        cc=cc,
                        bcc=bcc,
                        subject=subject,
                        body=body,
                        body_html=body_html,
                        in_reply_to=in_reply_to,
                        references=references,
                        existing_uid=None,
                        attachments=attachments,
                    )
                raise

            if cancellable.is_cancelled() and not appended_uid:
                if self._flushing_draft_queue:
                    raise RuntimeError("Draft save timed out")
                return self._queue_draft_unlocked(
                    account_uid,
                    folder_name,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    body=body,
                    body_html=body_html,
                    in_reply_to=in_reply_to,
                    references=references,
                    existing_uid=None,
                    attachments=attachments,
                )

            if not appended_uid:
                raise RuntimeError("Draft was saved but its UID could not be determined")
            return folder_name, appended_uid
        except GLib.Error as exc:
            if self._flushing_draft_queue:
                raise
            if cancellable.is_cancelled() or is_network_unavailable_error(exc):
                return _queue_local()
            raise
        finally:
            timer.cancel()
            if own_cancellable and cancellable.is_cancelled():
                log.debug(
                    "Draft save cancelled or timed out after %ds account=%s",
                    _DRAFT_TIMEOUT_SECONDS,
                    account_uid,
                )

    def delete_draft(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> None:
        run_on_mail_thread(
            self._delete_draft_unlocked,
            account_uid,
            folder_name,
            message_uid,
        )

    def _delete_draft_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> None:
        if is_queued_draft_id(message_uid):
            remove_queued_draft(message_uid)
            return
        with self._lock:
            self._delete_message_unlocked(account_uid, folder_name, message_uid)

    def get_correspondents(self, account_uid: str) -> list[Correspondent]:
        return run_on_mail_thread(self._get_correspondents_unlocked, account_uid)

    def _get_correspondents_unlocked(self, account_uid: str) -> list[Correspondent]:
        with self._lock:
            cached = self._correspondent_indexes.get(account_uid)
            if cached is not None:
                return cached
        correspondents = self._build_correspondents_index_unlocked(account_uid)
        with self._lock:
            self._correspondent_indexes[account_uid] = correspondents
        return correspondents

    def _folders_for_correspondents_unlocked(
        self, account_uid: str
    ) -> list[dict]:
        """Select Inbox/Sent from the cached folder tree only (no Camel connect)."""
        with self._lock:
            folders = self._folder_tree_cache.get(account_uid)
        if not folders:
            return []
        selected: list[dict] = []
        seen: set[str] = set()
        for folder_type, fallbacks in (
            (Camel.FolderInfoFlags.TYPE_INBOX, frozenset({"inbox"})),
            (
                Camel.FolderInfoFlags.TYPE_SENT,
                frozenset({"sent", "sent mail", "sent messages"}),
            ),
        ):
            info = find_folder_by_type(
                folders,
                folder_type,
                type_mask=Camel.FOLDER_TYPE_MASK,
                name_fallbacks=fallbacks,
            )
            if info is None:
                continue
            full_name = info.get("full_name")
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            selected.append(info)
        if selected:
            return selected
        for folder in folders:
            if not folder_can_contain_messages(folder):
                continue
            full_name = folder.get("full_name")
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            selected.append(folder)
            if len(selected) >= 3:
                break
        return selected

    def _build_correspondents_index_unlocked(
        self, account_uid: str
    ) -> list[Correspondent]:
        """Build autocomplete from in-memory / disk folder indexes only (#156)."""
        account = self.get_account(account_uid)
        exclude_emails: set[str] = set()
        for raw in (account.from_address, account.email):
            if raw:
                exclude_emails.add(normalize_email(raw))

        folders = self._folders_for_correspondents_unlocked(account_uid)
        messages: list[dict] = []
        for folder in folders:
            full_name = folder.get("full_name")
            if not full_name:
                continue
            with self._lock:
                index = self._folder_indexes.get((account_uid, full_name))
            if index is not None:
                messages.extend(index.messages)
                continue
            cached = folder_index_cache.load(account_uid, full_name)
            if cached is not None:
                cached_messages, _unread, _total = cached
                messages.extend(cached_messages)

        messages.sort(key=lambda message: message.get("sort_date") or 0, reverse=True)
        correspondents = collect_correspondents(messages, exclude_emails=exclude_emails)
        return correspondents[:_MAX_CORRESPONDENTS]

    def list_folders(
        self,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> list[dict]:
        if is_mail_io_thread():
            return self._list_folders_unlocked(
                account_uid, cancellable=cancellable
            )
        return run_on_mail_thread(
            self._list_folders_unlocked,
            account_uid,
            cancellable=cancellable,
        )

    def _list_folders_from_local_store_unlocked(
        self, account_uid: str
    ) -> list[dict]:
        """Build a folder tree from Camel's on-disk cache when the server is unreachable."""
        with self._lock:
            store = self._get_store_unlocked(account_uid)
        self._sync_store_online_state_unlocked(store, account_uid)

        folders: list[dict] = []
        try:
            root = store.get_folder_info_sync(
                None, Camel.StoreGetFolderInfoFlags.RECURSIVE, None
            )
            if root is not None:
                walk_folder_info(root, folders)
                result = [folder for folder in folders if folder.get("full_name")]
                if result:
                    return result
        except GLib.Error as exc:
            if not is_network_unavailable_error(exc):
                log.debug(
                    "Local folder info unavailable for account %s",
                    account_uid,
                    exc_info=True,
                )

        if isinstance(store, Camel.OfflineStore):
            try:
                listed = store.dup_downsync_folders()
            except Exception:
                log.debug(
                    "dup_downsync_folders failed for account %s",
                    account_uid,
                    exc_info=True,
                )
                listed = []
            if listed:
                seen: set[str] = set()
                local_folders: list[dict] = []
                for folder in listed:
                    full_name = folder.get_full_name()
                    if not isinstance(full_name, str) or not full_name:
                        continue
                    if full_name in seen:
                        continue
                    seen.add(full_name)
                    display_name = folder.get_display_name()
                    local_folders.append(
                        {
                            "full_name": full_name,
                            "display_name": display_name or full_name,
                            "unread": -1,
                            "total": -1,
                            "flags": 0,
                        }
                    )
                if local_folders:
                    return local_folders

        for inbox_candidate in ("INBOX", "Inbox", "inbox"):
            try:
                folder = store.get_folder_sync(inbox_candidate, 0, None)
            except GLib.Error:
                continue
            if folder is None:
                continue
            full_name = folder.get_full_name() or inbox_candidate
            display_name = folder.get_display_name() or full_name
            return [
                {
                    "full_name": full_name,
                    "display_name": display_name,
                    "unread": -1,
                    "total": -1,
                    "flags": int(Camel.FolderInfoFlags.TYPE_INBOX),
                }
            ]

        return []

    def _list_folders_unlocked(
        self,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> list[dict]:
        if cancellable is not None and cancellable.is_cancelled():
            search_trace("folder_list_cancelled", account=account_uid)
            raise GLib.Error.new_literal(
                Gio.io_error_quark(),
                "Operation was cancelled",
                Gio.IOErrorEnum.CANCELLED,
            )

        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)

        if not self._network_available:
            if cached is not None:
                return cached
            result = self._list_folders_from_local_store_unlocked(account_uid)
            if result:
                with self._lock:
                    self._folder_tree_cache[account_uid] = result
            return result

        try:
            with self._lock:
                store = self._get_store_unlocked(account_uid, cancellable=cancellable)
            root = store.get_folder_info_sync(
                None,
                Camel.StoreGetFolderInfoFlags.RECURSIVE,
                cancellable,
            )
            if cancellable is not None and cancellable.is_cancelled():
                search_trace("folder_list_cancelled", account=account_uid)
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(),
                    "Operation was cancelled",
                    Gio.IOErrorEnum.CANCELLED,
                )
            # Camel M365 can fail without setting GError (#156); do not cache [].
            if root is None:
                log.warning(
                    "Folder list failed for account %s without GError; "
                    "keeping prior cache if any",
                    account_uid,
                )
                if cached is not None:
                    return list(cached)
                result = self._list_folders_from_local_store_unlocked(account_uid)
                if result:
                    with self._lock:
                        self._folder_tree_cache[account_uid] = result
                    return result
                raise RuntimeError(
                    "Could not list folders for this account"
                )
            folders: list[dict] = []
            walk_folder_info(root, folders)
            result = [f for f in folders if f.get("full_name")]
            with self._lock:
                self._folder_tree_cache[account_uid] = result
            return result
        except GLib.Error as exc:
            if cancellable is not None and exc.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
            ):
                search_trace("folder_list_cancelled", account=account_uid)
                raise
            if cached is not None and is_network_unavailable_error(exc):
                log.debug(
                    "Using cached folder list for account %s while offline",
                    account_uid,
                )
                return cached
            if is_network_unavailable_error(exc):
                result = self._list_folders_from_local_store_unlocked(account_uid)
                if result:
                    with self._lock:
                        self._folder_tree_cache[account_uid] = result
                    return result
            raise

    def get_folder_stats(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        """Return live (unread, total) counts by opening the folder."""
        return run_on_mail_thread(
            self._get_folder_stats_unlocked, account_uid, folder_name
        )

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
        return run_on_mail_thread(
            self._archive_read_messages_unlocked, account_uid, folder_name
        )

    def count_read_unflagged_messages(
        self, account_uid: str, folder_name: str
    ) -> int:
        return run_on_mail_thread(
            self._count_read_unflagged_messages_unlocked,
            account_uid,
            folder_name,
        )

    def archive_read_unflagged_messages(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._archive_read_unflagged_messages_unlocked,
            account_uid,
            folder_name,
        )

    def archive_all_messages(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._archive_all_messages_unlocked, account_uid, folder_name
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
        uids = folder_get_uids(folder)
        deleted = Camel.MessageFlags.DELETED
        batch_size = 50
        for offset in range(0, len(uids), batch_size):
            batch = uids[offset : offset + batch_size]
            for message_uid in batch:
                folder.set_message_flags(camel_uid_to_api(message_uid), deleted, deleted)
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

    def _archive_all_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        index = self._build_folder_index_unlocked(account_uid, folder_name)
        all_uids = [
            message["uid"]
            for message in index.messages
            if message.get("uid")
        ]
        if not all_uids:
            return {
                "archived_count": 0,
                "source_folder_unread": index.unread,
                "source_folder_total": index.total,
            }
        result = self._archive_messages_unlocked(
            account_uid, folder_name, all_uids
        )
        result["archived_count"] = len(all_uids)
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
        folder_index_cache.invalidate_account(account_uid)

    def _cached_folder_stats_unlocked(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int] | None:
        key = (account_uid, folder_name)
        index = self._folder_indexes.get(key)
        if index is not None:
            return index.unread, index.total
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is not None:
            _messages, unread, total = cached
            return unread, total
        return None

    def _get_folder_stats_unlocked(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        with self._lock:
            store = self._get_store_unlocked(account_uid)
            folder = store.get_folder_sync(folder_name, 0, None)
            if folder is None:
                raise ValueError(f"Folder not found: {folder_name}")
            if not self._network_available:
                cached = self._cached_folder_stats_unlocked(account_uid, folder_name)
                if cached is not None:
                    return cached
            try:
                folder.refresh_info_sync(None)
            except GLib.Error as exc:
                if is_network_unavailable_error(exc):
                    cached = self._cached_folder_stats_unlocked(
                        account_uid, folder_name
                    )
                    if cached is not None:
                        return cached
                raise
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
        if is_mail_io_thread():
            return self._list_messages_page_unlocked(
                account_uid,
                folder_name,
                offset=offset,
                limit=limit,
                sync=sync,
            )
        return get_mail_io_thread().run_sync(
            self._list_messages_page_unlocked,
            account_uid,
            folder_name,
            offset=offset,
            limit=limit,
            sync=sync,
        )

    def _get_folder_messages_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        sync: bool,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        with self._lock:
            index, source = self._get_folder_index_unlocked(
                account_uid, folder_name, sync=sync
            )
            return list(index.messages), index.unread, index.total, source

    def get_folder_messages(
        self,
        account_uid: str,
        folder_name: str,
        *,
        sync: bool = True,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        if is_mail_io_thread():
            return self._get_folder_messages_unlocked(
                account_uid, folder_name, sync=sync
            )
        return get_mail_io_thread().run_sync(
            self._get_folder_messages_unlocked,
            account_uid,
            folder_name,
            sync=sync,
        )

    def _ordered_searchable_folders_unlocked(self, account_uid: str) -> list[dict]:
        folders = self._list_folders_unlocked(account_uid)
        searchable = [
            folder
            for folder in folders
            if folder_can_contain_messages(folder)
            and not is_post_outbox_folder(folder.get("full_name"))
        ]
        priority: list[dict] = []
        seen: set[str] = set()
        for folder_type, fallbacks in (
            (Camel.FolderInfoFlags.TYPE_INBOX, frozenset({"inbox"})),
            (
                Camel.FolderInfoFlags.TYPE_SENT,
                frozenset({"sent", "sent mail", "sent messages"}),
            ),
        ):
            info = find_folder_by_type(
                searchable,
                folder_type,
                type_mask=Camel.FOLDER_TYPE_MASK,
                name_fallbacks=fallbacks,
            )
            if info is None:
                continue
            full_name = info.get("full_name")
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            priority.append(info)
        remaining = sorted(
            (
                folder
                for folder in searchable
                if (folder.get("full_name") or "") not in seen
            ),
            key=lambda folder: str(folder.get("full_name") or ""),
        )
        return priority + remaining

    def _prepare_single_folder_search_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
    ) -> tuple[list[dict], int, FolderIndexSource, Any | None, bool]:
        with self._lock:
            key = (account_uid, folder_name)
            index = self._folder_indexes.get(key)
            if index is None:
                search_trace(
                    "search_index_load",
                    account=account_uid,
                    folder=folder_name,
                    source="missing_memory",
                )
                index, source = self._get_folder_index_unlocked(
                    account_uid, folder_name, sync=False
                )
            else:
                source = "memory"
            messages = list(index.messages)
            unread = index.unread

        needs_body = query_requires_body_scan(query)
        folder = (
            self._try_get_folder_for_search_unlocked(account_uid, folder_name)
            if needs_body
            else None
        )
        return messages, unread, source, folder, needs_body

    def _body_text_for_uid_loader(
        self,
        folder: Any | None,
        *,
        needs_body: bool,
        cancellable: Gio.Cancellable,
    ) -> Callable[[str], str | None] | None:
        if not needs_body:
            return None
        from .helpers import extract_message_bodies, searchable_body_text

        def body_text_for_uid(uid: str) -> str | None:
            if folder is None or cancellable.is_cancelled():
                return None
            try:
                api_uid = camel_uid_to_api(uid)
                mime = folder.get_message_cached(api_uid, None)
                if mime is None:
                    return None
            except Exception:
                return None
            bodies = extract_message_bodies(mime)
            return searchable_body_text(
                plain=bodies.get("plain"),
                html=bodies.get("html"),
            )

        return body_text_for_uid

    def _search_single_folder_index_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        cancellable: Gio.Cancellable,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, FolderIndexSource]:
        messages, unread, source, folder, needs_body = (
            self._prepare_single_folder_search_unlocked(
                account_uid,
                folder_name,
                query,
            )
        )
        search_trace(
            "search_filter_begin",
            account=account_uid,
            folder=folder_name,
            message_count=len(messages),
            needs_body=needs_body,
            has_folder=folder is not None,
            source=source,
        )

        filtered = filter_messages_by_query(
            messages,
            query,
            body_text_for_uid=self._body_text_for_uid_loader(
                folder,
                needs_body=needs_body,
                cancellable=cancellable,
            ),
            is_cancelled=cancellable.is_cancelled,
            on_progress=on_progress,
            on_matches=on_matches,
        )
        if cancellable.is_cancelled():
            search_trace(
                "search_filter_cancelled",
                account=account_uid,
                folder=folder_name,
            )
            return [], unread, source

        search_trace(
            "search_filter_result",
            account=account_uid,
            folder=folder_name,
            match_count=len(filtered),
        )
        return filtered, unread, source

    def _search_folder_messages_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        sync: bool,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        cancellable = self._begin_folder_search_unlocked()
        try:
            with search_trace_timer(
                "search_folder",
                account=account_uid,
                folder=folder_name,
                terms=len(query.terms),
            ):
                filtered, unread, source = self._search_single_folder_index_unlocked(
                    account_uid,
                    folder_name,
                    query,
                    cancellable=cancellable,
                    on_progress=on_progress,
                    on_matches=on_matches,
                )
                if cancellable.is_cancelled():
                    return [], unread, 0, source
                return filtered, unread, len(filtered), source
        finally:
            self._end_folder_search_unlocked(cancellable)

    def _search_folder_jobs_unlocked(
        self,
        folder_jobs: list[tuple[str, str, str]],
        query: MessageSearchQuery,
        *,
        cancellable: Gio.Cancellable,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        folders_total = len(folder_jobs)
        matched: list[dict] = []

        def report_folder_progress(
            progress: SearchFilterProgress,
            *,
            account_label: str,
            folder_label: str,
            folders_done: int,
            matched_before_folder: int,
        ) -> None:
            if on_progress is None:
                return
            on_progress(
                SearchFilterProgress(
                    progress.scanned,
                    progress.message_count,
                    matched_before_folder + progress.matches,
                    account_label=account_label,
                    folder_label=folder_label,
                    folders_done=folders_done,
                    folders_total=folders_total,
                )
            )

        for folders_done, (account_uid, folder_name, folder_label) in enumerate(
            folder_jobs, start=1
        ):
            if cancellable.is_cancelled():
                break
            account_label = self.get_account(account_uid).display_label
            matched_before_folder = len(matched)

            def folder_on_matches(
                batch: list[dict],
                *,
                _account_uid: str = account_uid,
                _folder_name: str = folder_name,
            ) -> None:
                if not batch or on_matches is None:
                    return
                annotated = [
                    annotate_search_match(
                        message,
                        account_uid=_account_uid,
                        folder_name=_folder_name,
                    )
                    for message in batch
                ]
                matched.extend(annotated)
                on_matches(annotated)

            filtered, _unread, _source = self._search_single_folder_index_unlocked(
                account_uid,
                folder_name,
                query,
                cancellable=cancellable,
                on_progress=lambda progress, al=account_label, fl=folder_label, fd=folders_done, mbf=matched_before_folder: report_folder_progress(
                    progress,
                    account_label=al,
                    folder_label=fl,
                    folders_done=fd,
                    matched_before_folder=mbf,
                ),
                on_matches=folder_on_matches,
            )
            if cancellable.is_cancelled():
                break
            if on_matches is None:
                matched.extend(
                    annotate_search_match(
                        message,
                        account_uid=account_uid,
                        folder_name=folder_name,
                    )
                    for message in filtered
                )

        if cancellable.is_cancelled():
            return [], 0, 0, "memory"

        sorted_matches = sort_messages_newest_first(matched)
        match_count = len(sorted_matches)
        return sorted_matches, 0, match_count, "memory"

    def _folder_jobs_for_all_unlocked(self) -> list[tuple[str, str, str]]:
        folder_jobs: list[tuple[str, str, str]] = []
        for account in self.list_accounts():
            for folder in self._ordered_searchable_folders_unlocked(account.uid):
                full_name = folder.get("full_name")
                if not full_name:
                    continue
                display = folder.get("display_name") or full_name
                folder_jobs.append((account.uid, full_name, display))
        return folder_jobs

    def _folder_jobs_for_account_unlocked(
        self, account_uid: str
    ) -> list[tuple[str, str, str]]:
        folder_jobs: list[tuple[str, str, str]] = []
        for folder in self._ordered_searchable_folders_unlocked(account_uid):
            full_name = folder.get("full_name")
            if not full_name:
                continue
            display = folder.get("display_name") or full_name
            folder_jobs.append((account_uid, full_name, display))
        return folder_jobs

    def _start_search_folder_jobs(
        self,
        folder_jobs: list[tuple[str, str, str]],
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
        on_complete: SearchCompleteCallback | None = None,
    ) -> None:
        """Search one folder per mail-I/O task so interactive reads can interleave."""
        cancellable = self._begin_folder_search_unlocked()
        matched: list[dict] = []
        folders_total = len(folder_jobs)
        state: dict[str, Any] = {
            "index": 0,
            "folder_ctx": None,
            "scan_cursor": None,
        }

        def finish(*, cancelled: bool = False) -> None:
            try:
                if cancelled or cancellable.is_cancelled():
                    if on_complete is not None:
                        on_complete(([], 0, 0, "memory"))
                    return
                sorted_matches = sort_messages_newest_first(matched)
                match_count = len(sorted_matches)
                if on_complete is not None:
                    on_complete((sorted_matches, 0, match_count, "memory"))
            finally:
                self._end_folder_search_unlocked(cancellable)

        def report_folder_progress(
            progress: SearchFilterProgress,
            *,
            account_label: str,
            folder_label: str,
            folders_done: int,
            matched_before_folder: int,
        ) -> None:
            if on_progress is None:
                return
            on_progress(
                SearchFilterProgress(
                    progress.scanned,
                    progress.message_count,
                    matched_before_folder + progress.matches,
                    account_label=account_label,
                    folder_label=folder_label,
                    folders_done=folders_done,
                    folders_total=folders_total,
                )
            )

        def run_folder() -> None:
            if cancellable.is_cancelled():
                finish(cancelled=True)
                return
            folder_index = state["index"]
            if folder_index >= len(folder_jobs):
                finish()
                return

            account_uid, folder_name, folder_label = folder_jobs[folder_index]
            account_label = self.get_account(account_uid).display_label
            folders_done = folder_index + 1
            matched_before_folder = len(matched)

            if state["folder_ctx"] is None:
                messages, unread, source, folder, needs_body = (
                    self._prepare_single_folder_search_unlocked(
                        account_uid,
                        folder_name,
                        query,
                    )
                )
                state["folder_ctx"] = (
                    messages,
                    unread,
                    source,
                    folder,
                    needs_body,
                )
                state["scan_cursor"] = SearchScanCursor()
                search_trace(
                    "search_filter_begin",
                    account=account_uid,
                    folder=folder_name,
                    message_count=len(messages),
                    needs_body=needs_body,
                    has_folder=folder is not None,
                    source=source,
                )

            messages, _unread, _source, folder, needs_body = state["folder_ctx"]
            cursor = state["scan_cursor"]
            assert isinstance(cursor, SearchScanCursor)

            def folder_on_matches(
                batch: list[dict],
                *,
                _account_uid: str = account_uid,
                _folder_name: str = folder_name,
            ) -> None:
                if not batch or on_matches is None:
                    return
                annotated = [
                    annotate_search_match(
                        message,
                        account_uid=_account_uid,
                        folder_name=_folder_name,
                    )
                    for message in batch
                ]
                matched.extend(annotated)
                on_matches(annotated)

            filter_messages_by_query(
                messages,
                query,
                body_text_for_uid=self._body_text_for_uid_loader(
                    folder,
                    needs_body=needs_body,
                    cancellable=cancellable,
                ),
                is_cancelled=cancellable.is_cancelled,
                should_yield=get_mail_io_thread().has_interactive_work_pending,
                cursor=cursor,
                on_progress=lambda progress, al=account_label, fl=folder_label, fd=folders_done, mbf=matched_before_folder: report_folder_progress(
                    progress,
                    account_label=al,
                    folder_label=fl,
                    folders_done=fd,
                    matched_before_folder=mbf,
                ),
                on_matches=folder_on_matches,
            )
            if cancellable.is_cancelled():
                finish(cancelled=True)
                return
            if cursor.index < len(messages):
                get_mail_io_thread().submit(run_folder)
                return

            state["folder_ctx"] = None
            state["scan_cursor"] = None
            state["index"] = folder_index + 1
            get_mail_io_thread().submit(run_folder)

        if is_mail_io_thread():
            run_folder()
        else:
            get_mail_io_thread().submit_front(run_folder)

    def start_search_all_messages(
        self,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
        on_complete: SearchCompleteCallback | None = None,
    ) -> None:
        if is_mail_io_thread():
            folder_jobs = self._folder_jobs_for_all_unlocked()
            self._start_search_folder_jobs(
                folder_jobs,
                query,
                on_progress=on_progress,
                on_matches=on_matches,
                on_complete=on_complete,
            )
            return
        get_mail_io_thread().submit_front(
            self.start_search_all_messages,
            query,
            on_progress=on_progress,
            on_matches=on_matches,
            on_complete=on_complete,
        )

    def start_search_account_messages(
        self,
        account_uid: str,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
        on_complete: SearchCompleteCallback | None = None,
    ) -> None:
        if is_mail_io_thread():
            folder_jobs = self._folder_jobs_for_account_unlocked(account_uid)
            self._start_search_folder_jobs(
                folder_jobs,
                query,
                on_progress=on_progress,
                on_matches=on_matches,
                on_complete=on_complete,
            )
            return
        get_mail_io_thread().submit_front(
            self.start_search_account_messages,
            account_uid,
            query,
            on_progress=on_progress,
            on_matches=on_matches,
            on_complete=on_complete,
        )

    def start_search_folder_messages(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
        on_complete: SearchCompleteCallback | None = None,
    ) -> None:
        if is_mail_io_thread():
            display = folder_name
            for folder in self._ordered_searchable_folders_unlocked(account_uid):
                if folder.get("full_name") == folder_name:
                    display = folder.get("display_name") or folder_name
                    break
            self._start_search_folder_jobs(
                [(account_uid, folder_name, display)],
                query,
                on_progress=on_progress,
                on_matches=on_matches,
                on_complete=on_complete,
            )
            return
        get_mail_io_thread().submit_front(
            self.start_search_folder_messages,
            account_uid,
            folder_name,
            query,
            on_progress=on_progress,
            on_matches=on_matches,
            on_complete=on_complete,
        )

    def _search_all_messages_unlocked(
        self,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        cancellable = self._begin_folder_search_unlocked()
        try:
            with search_trace_timer("search_all", terms=len(query.terms)):
                folder_jobs = self._folder_jobs_for_all_unlocked()
                return self._search_folder_jobs_unlocked(
                    folder_jobs,
                    query,
                    cancellable=cancellable,
                    on_progress=on_progress,
                    on_matches=on_matches,
                )
        finally:
            self._end_folder_search_unlocked(cancellable)

    def _search_account_messages_unlocked(
        self,
        account_uid: str,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        cancellable = self._begin_folder_search_unlocked()
        try:
            with search_trace_timer(
                "search_account",
                account=account_uid,
                terms=len(query.terms),
            ):
                folder_jobs = self._folder_jobs_for_account_unlocked(account_uid)
                return self._search_folder_jobs_unlocked(
                    folder_jobs,
                    query,
                    cancellable=cancellable,
                    on_progress=on_progress,
                    on_matches=on_matches,
                )
        finally:
            self._end_folder_search_unlocked(cancellable)

    def search_all_messages(
        self,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        if is_mail_io_thread():
            return self._search_all_messages_unlocked(
                query,
                on_progress=on_progress,
                on_matches=on_matches,
            )
        return get_mail_io_thread().run_sync(
            self._search_all_messages_unlocked,
            query,
            on_progress=on_progress,
            on_matches=on_matches,
        )

    def search_account_messages(
        self,
        account_uid: str,
        query: MessageSearchQuery,
        *,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        if is_mail_io_thread():
            return self._search_account_messages_unlocked(
                account_uid,
                query,
                on_progress=on_progress,
                on_matches=on_matches,
            )
        return get_mail_io_thread().run_sync(
            self._search_account_messages_unlocked,
            account_uid,
            query,
            on_progress=on_progress,
            on_matches=on_matches,
        )

    def search_folder_messages(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        sync: bool = False,
        on_progress: SearchProgressCallback | None = None,
        on_matches: SearchMatchCallback | None = None,
    ) -> tuple[list[dict], int, int, FolderIndexSource]:
        if is_mail_io_thread():
            return self._search_folder_messages_unlocked(
                account_uid,
                folder_name,
                query,
                sync=sync,
                on_progress=on_progress,
                on_matches=on_matches,
            )
        return get_mail_io_thread().run_sync(
            self._search_folder_messages_unlocked,
            account_uid,
            folder_name,
            query,
            sync=sync,
            on_progress=on_progress,
            on_matches=on_matches,
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
        sync: bool,
    ) -> tuple[list[dict], int, int, bool]:
        with self._lock:
            index, _source = self._get_folder_index_unlocked(
                account_uid, folder_name, sync=sync
            )
            page, has_more = paginate_messages(index.messages, offset, limit)
            return page, index.unread, index.total, has_more

    def _get_folder_index_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        sync: bool,
    ) -> tuple[_FolderMessageIndex, FolderIndexSource]:
        key = (account_uid, folder_name)
        if sync:
            index = self._build_folder_index_unlocked(
                account_uid, folder_name, sync=True
            )
            self._folder_indexes[key] = index
            if _folder_index_is_cacheable(index):
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    index.messages,
                    index.unread,
                    index.total,
                )
            return index, "server"

        index = self._folder_indexes.get(key)
        if index is not None:
            return index, "memory"

        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is not None:
            messages, unread, total = cached
            index = _FolderMessageIndex(
                messages=messages,
                unread=unread,
                total=total,
            )
            self._folder_indexes[key] = index
            return index, "disk_cache"

        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
        self._folder_indexes[key] = index
        if index.messages or index.total:
            if _folder_index_is_cacheable(index):
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    index.messages,
                    index.unread,
                    index.total,
                )
        return index, "local"

    def _is_missing_folder_error(self, exc: GLib.Error) -> bool:
        return exc.matches(Camel.store_error_quark(), Camel.StoreError.NO_FOLDER)

    def _is_missing_message_error(self, exc: GLib.Error) -> bool:
        return exc.matches(
            Camel.folder_error_quark(), Camel.FolderError.INVALID_UID
        )

    def _folder_search_uids_unlocked(
        self,
        folder: Camel.Folder,
        expression: str,
        uids: list[str],
        cancellable: Gio.Cancellable | None = None,
    ) -> set[str]:
        if not uids:
            return set()
        if cancellable is not None and cancellable.is_cancelled():
            return set()
        matches = folder_search_uids(
            folder,
            expression,
            uids,
            cancellable=cancellable,
        )
        return set(matches)

    @staticmethod
    def _index_message_uids(index: _FolderMessageIndex) -> list[str]:
        return [str(msg["uid"]) for msg in index.messages if msg.get("uid")]

    def _get_message_mime_sync(
        self,
        folder: Camel.Folder,
        folder_name: str,
        message_uid: str,
    ) -> Any:
        offline = not self._network_available
        mime = None
        api_uid = camel_uid_to_api(message_uid)
        try:
            mime = folder.get_message_sync(api_uid, None)
        except GLib.Error as exc:
            if self._is_missing_message_error(exc):
                if offline:
                    mime = folder.get_message_cached(api_uid, None)
                    if mime is not None:
                        return mime
                    raise MessageNotAvailableError(
                        message_uid,
                        folder_name,
                        reason=MessageUnavailableReason.NOT_CACHED_OFFLINE,
                    ) from exc
                raise MessageNotAvailableError(message_uid, folder_name) from exc
            raise
        if mime is None:
            if offline:
                mime = folder.get_message_cached(api_uid, None)
                if mime is not None:
                    return mime
                raise MessageNotAvailableError(
                    message_uid,
                    folder_name,
                    reason=MessageUnavailableReason.NOT_CACHED_OFFLINE,
                )
            raise MessageNotAvailableError(message_uid, folder_name)
        return mime

    def _open_folder_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> Camel.Folder | None:
        store = self._get_store_unlocked(account_uid, cancellable=cancellable)
        try:
            return store.get_folder_sync(folder_name, 0, cancellable)
        except GLib.Error as exc:
            if self._is_missing_folder_error(exc):
                log.debug(
                    "Skipping unavailable folder %r for account %s",
                    folder_name,
                    account_uid,
                )
                return None
            raise

    def _try_get_folder_for_search_unlocked(
        self, account_uid: str, folder_name: str
    ) -> Camel.Folder | None:
        """Open a folder only when its store is already connected (no connect_sync)."""
        with self._lock:
            store = self._stores.get(account_uid)
        if store is None:
            search_trace(
                "search_folder_skip",
                account=account_uid,
                folder=folder_name,
                reason="store_not_open",
            )
            return None
        if (
            store.get_connection_status()
            != Camel.ServiceConnectionStatus.CONNECTED
        ):
            search_trace(
                "search_folder_skip",
                account=account_uid,
                folder=folder_name,
                reason="store_not_connected",
            )
            return None
        try:
            return store.get_folder_sync(folder_name, 0, None)
        except GLib.Error as exc:
            if self._is_missing_folder_error(exc):
                return None
            raise

    def _require_folder_unlocked(
        self, account_uid: str, folder_name: str
    ) -> Camel.Folder:
        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")
        from post.preferences import get_account_offline_body_sync

        apply_offline_sync_to_folder(folder, get_account_offline_body_sync(account_uid))
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

            uids = folder_get_uids(folder)
            if not uids:
                return _FolderMessageIndex(messages=[], unread=unread, total=total)

            messages: list[dict] = []
            for uid in uids:
                info = folder_get_message_info(folder, uid)
                if info is None:
                    continue
                try:
                    messages.append(message_info_to_dict(info, uid=uid))
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
        return run_on_mail_thread(
            self._read_message_unlocked,
            account_uid,
            folder_name,
            message_uid,
            mark_seen=mark_seen,
        )

    def read_attachment_data(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        attachment_index: int,
    ) -> tuple[str, bytes]:
        return run_on_mail_thread(
            self._read_attachment_data_unlocked,
            account_uid,
            folder_name,
            message_uid,
            attachment_index,
        )

    def toggle_message_seen(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._toggle_message_seen_unlocked,
            account_uid,
            folder_name,
            message_uid,
        )

    def toggle_message_flagged(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._toggle_message_flagged_unlocked,
            account_uid,
            folder_name,
            message_uid,
        )

    def set_messages_seen(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        seen: bool,
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._set_messages_seen_unlocked,
            account_uid,
            folder_name,
            message_uids,
            seen=seen,
        )

    def set_messages_flagged(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        flagged: bool,
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._set_messages_flagged_unlocked,
            account_uid,
            folder_name,
            message_uids,
            flagged=flagged,
        )

    def toggle_messages_seen(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._toggle_messages_seen_unlocked,
            account_uid,
            folder_name,
            message_uids,
        )

    def toggle_messages_flagged(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._toggle_messages_flagged_unlocked,
            account_uid,
            folder_name,
            message_uids,
        )

    def move_messages_to_trash(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._move_messages_to_trash_unlocked,
            account_uid,
            folder_name,
            message_uids,
        )

    def archive_messages(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._archive_messages_unlocked,
            account_uid,
            folder_name,
            message_uids,
        )

    def move_messages(
        self,
        account_uid: str,
        source_folder: str,
        destination_folder: str,
        message_uids: list[str],
    ) -> dict[str, Any]:
        return run_on_mail_thread(
            self._move_messages_unlocked,
            account_uid,
            source_folder,
            destination_folder,
            message_uids,
        )

    def mark_message_read(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> tuple[int, int]:
        return run_on_mail_thread(
            self._mark_message_read_unlocked,
            account_uid,
            folder_name,
            message_uid,
        )

    def _move_messages_unlocked(
        self,
        account_uid: str,
        source_folder: str,
        destination_folder: str,
        message_uids: list[str],
    ) -> dict[str, Any]:
        store = self._get_store_unlocked(account_uid)
        dest = store.get_folder_sync(destination_folder, 0, None)
        if dest is None:
            raise ValueError(f"Folder not found: {destination_folder}")
        return self._transfer_messages_unlocked(
            account_uid, source_folder, message_uids, dest, op_type="move_to_folder"
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
            enrich_message_dict_from_mime,
            extract_attachments,
            extract_inline_images,
            extract_message_bodies,
        )

        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        info = folder_get_message_info(folder,message_uid)
        was_unread = info is not None and not (
            info.get_flags() & Camel.MessageFlags.SEEN
        )

        mime = self._get_message_mime_sync(folder, folder_name, message_uid)

        result = message_info_to_dict(info) if info else {"uid": message_uid}
        enrich_message_dict_from_mime(result, mime)
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
                result["references"] = normalize_references_header(references)

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

        mime = self._get_message_mime_sync(folder, folder_name, message_uid)

        return get_attachment_data(mime, attachment_index)

    def _mark_message_read_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> tuple[int, int]:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        info = folder_get_message_info(folder,message_uid)
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
                account_uid, folder, uids, op_type="set_seen", seen=True
            ),
        )

    def _toggle_message_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed:
            queued = self._persist_message_flag_changes_unlocked(
                account_uid,
                folder,
                [message_uid],
                op_type="set_seen",
                seen=new_seen,
            )
        return {
            "flags": {"seen": new_seen},
            "folder_unread": unread,
            "folder_total": total,
            "queued": queued,
        }

    def _toggle_message_flagged_unlocked(
        self, account_uid: str, folder_name: str, message_uid: str
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed:
            queued = self._persist_message_flag_changes_unlocked(
                account_uid,
                folder,
                [message_uid],
                op_type="set_flagged",
                flagged=new_flagged,
            )
        return {"flags": {"flagged": new_flagged}, "queued": queued}

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
            info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed_uids:
            queued = self._persist_message_flag_changes_unlocked(
                account_uid,
                folder,
                changed_uids,
                op_type="set_seen",
                seen=seen,
            )
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "updates": updates,
            "folder_unread": unread,
            "folder_total": total,
            "queued": queued,
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
            info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed_uids:
            queued = self._persist_message_flag_changes_unlocked(
                account_uid,
                folder,
                changed_uids,
                op_type="set_flagged",
                flagged=flagged,
            )
        return {"updates": updates, "queued": queued}

    def _toggle_messages_seen_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed_uids:
            if not self._network_available and not self._flushing_operation_queue:
                for item in updates:
                    uid = item.get("uid")
                    if uid not in changed_uids:
                        continue
                    flags = item.get("flags") or {}
                    if "seen" not in flags:
                        continue
                    self._queue_flag_operation_unlocked(
                        account_uid,
                        folder_name,
                        [str(uid)],
                        op_type="set_seen",
                        seen=bool(flags["seen"]),
                    )
                queued = True
            else:
                queued = self._persist_message_flag_changes_unlocked(
                    account_uid,
                    folder,
                    changed_uids,
                    op_type="set_seen",
                    seen=bool(updates[0].get("flags", {}).get("seen"))
                    if updates
                    else True,
                )
        unread = folder.get_unread_message_count()
        total = folder.get_message_count()
        self._update_cached_folder_counts(account_uid, folder_name, unread, total)
        return {
            "updates": updates,
            "folder_unread": unread,
            "folder_total": total,
            "queued": queued,
        }

    def _toggle_messages_flagged_unlocked(
        self, account_uid: str, folder_name: str, message_uids: list[str]
    ) -> dict[str, Any]:
        folder = self._require_folder_unlocked(account_uid, folder_name)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder_get_message_info(folder,message_uid)
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
        queued = False
        if changed_uids:
            if not self._network_available and not self._flushing_operation_queue:
                for item in updates:
                    uid = item.get("uid")
                    if uid not in changed_uids:
                        continue
                    flags = item.get("flags") or {}
                    if "flagged" not in flags:
                        continue
                    self._queue_flag_operation_unlocked(
                        account_uid,
                        folder_name,
                        [str(uid)],
                        op_type="set_flagged",
                        flagged=bool(flags["flagged"]),
                    )
                queued = True
            else:
                queued = self._persist_message_flag_changes_unlocked(
                    account_uid,
                    folder,
                    changed_uids,
                    op_type="set_flagged",
                    flagged=bool(updates[0].get("flags", {}).get("flagged"))
                    if updates
                    else True,
                )
        return {"updates": updates, "queued": queued}

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
            account_uid, folder_name, message_uids, trash_folder, op_type="move_to_trash"
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
                camel_uid_to_api(message_uid), deleted_mask, deleted_mask
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
            account_uid, folder_name, message_uids, archive_folder, op_type="archive"
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
            if folder_get_message_info(folder,uid) is None:
                continue
            seen.add(uid)
            transfer_uids.append(uid)
        return transfer_uids

    def _optimistic_remove_messages_from_cache_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
    ) -> tuple[int, int]:
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is None:
            return -1, -1
        uid_set = set(message_uids)
        removed_unread = sum(
            1
            for message in index.messages
            if message.get("uid") in uid_set
            and not (message.get("flags") or {}).get("seen", False)
        )
        index.messages = [
            message
            for message in index.messages
            if message.get("uid") not in uid_set
        ]
        if index.unread >= 0:
            index.unread = max(0, index.unread - removed_unread)
        if index.total >= 0:
            index.total = max(0, index.total - len(message_uids))
        else:
            index.total = len(index.messages)
        folder_index_cache.save(
            account_uid,
            folder_name,
            index.messages,
            index.unread,
            index.total,
        )
        return index.unread, index.total

    def _queue_transfer_operation_unlocked(
        self,
        account_uid: str,
        source_folder_name: str,
        message_uids: list[str],
        *,
        op_type: OperationType,
        destination_folder: str | None,
    ) -> dict[str, Any]:
        source_unread, source_total = self._optimistic_remove_messages_from_cache_unlocked(
            account_uid, source_folder_name, message_uids
        )
        enqueue_operation(
            QueuedOperation(
                op_type=op_type,
                account_uid=account_uid,
                folder_name=source_folder_name,
                message_uids=list(message_uids),
                destination_folder=destination_folder,
            )
        )
        return {
            "moved_uids": list(message_uids),
            "destination_uids": [],
            "source_folder": source_folder_name,
            "source_folder_unread": source_unread,
            "source_folder_total": source_total,
            "destination_folder": destination_folder,
            "destination_folder_unread": -1,
            "destination_folder_total": -1,
            "queued": True,
        }

    def _queue_flag_operation_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uids: list[str],
        *,
        op_type: OperationType,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        enqueue_operation(
            QueuedOperation(
                op_type=op_type,
                account_uid=account_uid,
                folder_name=folder_name,
                message_uids=list(message_uids),
                seen=seen,
                flagged=flagged,
            )
        )

    def _transfer_messages_unlocked(
        self,
        account_uid: str,
        source_folder_name: str,
        message_uids: list[str],
        destination_folder: Camel.Folder,
        *,
        op_type: OperationType = "move_to_folder",
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

        if not self._network_available and not self._flushing_operation_queue:
            return self._queue_transfer_operation_unlocked(
                account_uid,
                source_folder_name,
                transfer_uids,
                op_type=op_type,
                destination_folder=dest_name,
            )

        source_messages = self._message_dicts_for_uids_unlocked(
            source_folder, transfer_uids
        )

        destination_uids: list[str] = []
        try:
            for offset in range(0, len(transfer_uids), _TRANSFER_MESSAGE_BATCH_SIZE):
                batch = transfer_uids[offset : offset + _TRANSFER_MESSAGE_BATCH_SIZE]
                ok, transferred = source_folder.transfer_messages_to_sync(
                    batch, destination_folder, True, None
                )
                if not ok:
                    raise RuntimeError("Could not move messages")
                destination_uids.extend(camel_uid_list(transferred))

            self._commit_folder_transfer_unlocked(source_folder, destination_folder)
        except Exception as exc:
            if is_queueable_network_error(exc) and not self._flushing_operation_queue:
                return self._queue_transfer_operation_unlocked(
                    account_uid,
                    source_folder_name,
                    transfer_uids,
                    op_type=op_type,
                    destination_folder=dest_name,
                )
            raise

        # Camel returns destination UIDs; the UI and cache use source UIDs.
        moved_uids = list(transfer_uids)
        try:
            source_folder.refresh_info_sync(None)
            destination_folder.refresh_info_sync(None)
        except GLib.Error as exc:
            if not is_network_unavailable_error(exc):
                raise

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
        *,
        op_type: OperationType | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> bool:
        if not message_uids:
            return False
        if not self._network_available and not self._flushing_operation_queue:
            if op_type is None:
                raise RuntimeError("Offline flag sync requires operation type")
            folder_name = folder.get_full_name()
            if not folder_name:
                raise RuntimeError("Could not determine folder for queued flag change")
            self._queue_flag_operation_unlocked(
                account_uid,
                folder_name,
                message_uids,
                op_type=op_type,
                seen=seen,
                flagged=flagged,
            )
            return True
        store = self._get_store_unlocked(account_uid)
        try:
            self._persist_folder_flags_unlocked(store, folder, message_uids)
        except Exception as exc:
            if (
                is_queueable_network_error(exc)
                and not self._flushing_operation_queue
                and op_type is not None
            ):
                folder_name = folder.get_full_name()
                if folder_name:
                    self._queue_flag_operation_unlocked(
                        account_uid,
                        folder_name,
                        message_uids,
                        op_type=op_type,
                        seen=seen,
                        flagged=flagged,
                    )
                    return True
            raise
        return False

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
            info = folder_get_message_info(folder,message_uid)
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
        uids = folder_get_uids(folder)
        if not uids:
            return []

        for uid in uids:
            info = folder_get_message_info(folder,uid)
            if info is None:
                continue
            message = message_info_to_dict(info, uid=uid)
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
            folder_index_cache.invalidate(account_uid, folder_name)

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        return guess_inbox_name(folders)
