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
from typing import Any, Literal, NoReturn, Sequence, Callable

FolderIndexSource = Literal["memory", "disk_cache", "local", "server"]

import gi

gi.require_version("Camel", "1.2")
gi.require_version("EDataServer", "1.2")
gi.require_version("Gio", "2.0")

from gi.repository import Camel, EDataServer, GLib, Gio

from . import correspondent_cache
from . import folder_index_cache
from . import folder_status_cache
from . import graph_folder_counts
from .account_cache_gc import drop_orphan_account_caches
from .evolution_cache_path import (
    cached_rfc822_candidates,
    evolution_store_roots,
    find_nonempty_rfc822,
    first_nonempty_path,
    rfc822_digest,
)
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
    camel_uid_is_binary,
    camel_uid_to_api,
    camel_uid_list,
    folder_get_message_info,
    folder_get_uids,
    folder_get_unread_count,
    folder_search_all_uids,
    folder_search_uids,
    normalize_camel_uid,
)
from .message_flags import (
    apply_message_flags as _apply_message_flags,
    apply_message_flagged as _apply_message_flagged,
    mark_message_seen as _mark_message_seen,
    message_info_is_flagged as _message_info_is_flagged,
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
from .network_errors import (
    MESSAGE_NOT_CACHED_SIGN_IN,
    is_network_unavailable_error,
    is_queueable_network_error,
    is_sign_in_required_error,
    log_mail_error,
)
from .send_queue import (
    QueuedOutboundMessage,
    enqueue_outbound_message,
    is_outbound_ready_to_send,
    list_queued_outbound_messages,
    load_queued_attachments,
    load_queued_outbound_message,
    remove_queued_outbound_message,
)
from post.preferences import get_show_evolution_local
from .account_status import AccountConnectHealth, AccountTransferState
from .auth import (
    PasswordPromptCallback,
    authenticate_service_sync,
    authentication_failed_error,
    ensure_goa_credentials,
    open_gnome_online_accounts,
    source_uses_goa,
)
from .compose import (
    ComposeAttachment,
    addresses_to_internet_address,
    build_draft_mime_message,
    build_plain_mime_message,
    new_outbound_mime_identifiers,
    normalize_references_header,
    read_compose_attachments_from_message,
)
from .correspondents import (
    Correspondent,
    collect_correspondents,
    folder_feeds_correspondents,
    merge_correspondents,
)
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
from .message_list_state import (
    HEAVY_FOLDER_INDEX_BATCH_SIZE,
    find_folder_index_sibling_uids,
    folder_index_covers_identities,
    is_heavy_folder_name,
    is_trash_or_junk_folder_name,
    prune_stale_folder_index_uids,
    union_folder_index_messages,
    upsert_folder_index_by_identity,
)
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

# EDS also lists RSS feeds, search folders, etc. as "Mail Account" sources.
_SKIP_BACKENDS = frozenset({"rss", "vfolder"})
DEFAULT_MESSAGE_PAGE_SIZE = 50
_SEND_TIMEOUT_SECONDS = 30
_DRAFT_TIMEOUT_SECONDS = 30
# Camel transfer_messages_to_sync can hang on M365 after Graph already moved
# the message. Bound the call and soft-succeed when source UIDs are gone.
_TRANSFER_TIMEOUT_SECONDS = 45
# Post-transfer synchronize/refresh must not pin the mail I/O thread forever
# after Camel has already moved messages (M365 Graph can hang here).
_TRANSFER_POST_TIMEOUT_SECONDS = 30
# Per-folder refresh_info_sync / store FolderInfo REFRESH for sidebar counts
# must not pin post-mail-io (#197, #210).
_FOLDER_STATS_TIMEOUT_SECONDS = 15
# Bound get_message_sync so revoked GOA tokens cannot leave the reader hung (#341).
_MESSAGE_READ_TIMEOUT_SECONDS = 30
# Stay below Camel IMAPx MAX_UIDSET_ITEMS (100) to avoid spurious uidset warnings
# in evolution-data-server 3.56 when batching UID MOVE/COPY commands.
_TRANSFER_MESSAGE_BATCH_SIZE = 50


def _run_on_gtk_thread(callback: Callable[[], None]) -> bool:
    callback()
    return False


class MessageUnavailableReason:
    VANISHED = "vanished"
    NOT_CACHED_OFFLINE = "not_cached_offline"
    NOT_CACHED_SIGN_IN = "not_cached_sign_in"


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
        if self.reason == MessageUnavailableReason.NOT_CACHED_SIGN_IN:
            return MESSAGE_NOT_CACHED_SIGN_IN
        return "This message is no longer available."


def _merge_heavy_folder_status_into_tree(
    account_uid: str, folders: list[dict]
) -> list[dict]:
    """Apply STATUS high-water; blank summary-sized heavy-folder totals (#208)."""
    merged: list[dict] = []
    for folder in folders:
        item = dict(folder)
        name = item.get("full_name")
        if isinstance(name, str) and is_heavy_folder_name(name):
            unread = int(item.get("unread", -1))
            total = int(item.get("total", -1))
            # list_folders without REFRESH is untrusted (often Camel summary).
            folder_status_cache.observe(
                account_uid, name, unread, total, trusted=False
            )
            kept_unread, kept_total = folder_status_cache.resolve_sidebar(
                account_uid, name, unread, total
            )
            item["unread"] = kept_unread
            item["total"] = kept_total
        merged.append(item)
    return merged


def _apply_heavy_status_high_water(
    account_uid: str, stats: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int]]:
    """Merge store FolderInfo REFRESH counts into grow-only STATUS high-water."""
    out: dict[str, tuple[int, int]] = {}
    for name, (unread, total) in stats.items():
        if is_heavy_folder_name(name):
            local_indexed: int | None = None
            cached = folder_index_cache.load(account_uid, name)
            if cached is not None:
                local_indexed = len(cached[0])
            # Scrub poisoned summary-echo high-water before merging.
            if local_indexed is not None:
                folder_status_cache.scrub_if_summary_echo(
                    account_uid, name, local_indexed
                )
            folder_status_cache.observe(
                account_uid,
                name,
                unread,
                total,
                trusted=True,
                local_indexed=local_indexed,
            )
            out[name] = folder_status_cache.resolve_sidebar(
                account_uid, name, unread, total
            )
        else:
            out[name] = (unread, total)
    return out


@dataclass
class _FolderMessageIndex:
    messages: list[dict]
    unread: int
    total: int


@dataclass
class HeavyFolderIndexProgress:
    """One slice of a preemptible heavy-folder header index (#208)."""

    messages: list[dict]
    unread: int
    total: int
    done: bool
    cursor: dict[str, Any] = field(default_factory=dict)
    # RestId remaps from identity upsert (old UID → new UID) for UI selection (#267).
    uid_remaps: dict[str, str] = field(default_factory=dict)


# Heavy-folder Graph refresh follows Evolution's model: run refresh_info to
# completion (full M365 delta). Soft-cancelling mid-flight often yields zero
# new UIDs. Cancel only when leaving the folder (#208).
# Log a heartbeat while waiting on Graph so post.log shows live progress (#208).
_HEAVY_FOLDER_REFRESH_HEARTBEAT_SECONDS = 2.0
# After a successful refresh that adds no new UIDs, retry with
# prepare_content_refresh this many times before giving up (#208).
_HEAVY_FOLDER_REFRESH_STALL_LIMIT = 8
# One completed refresh_info per slice, then materialize and return to the UI.
_HEAVY_FOLDER_REFRESH_PAGES_PER_SLICE = 1
_HEAVY_FOLDER_REFRESH_SLICE_BUDGET_SECONDS = 960.0
# Skip prepare_content_refresh once the local index is already substantial —
# resetting M365 sync state throws away progress (#208).
_HEAVY_FOLDER_PREPARE_MIN_INDEXED = 500
# Camel summary clearly short of Graph/STATUS total → incomplete delta (#208).
# Mid-refresh ``new=0`` here is usually a full-delta rewalk of known UIDs, not a
# hang. Empty refresh finishes while still behind → force prepare + keep alive.
_HEAVY_FOLDER_INCOMPLETE_CAMEL_GAP = 100
# Cap how often we clear the M365 delta link while still behind (#208).
_HEAVY_FOLDER_INCOMPLETE_PREPARE_LIMIT = 3
# How often to re-publish Syncing progress during a silent rewalk (#208).
_HEAVY_FOLDER_REWALK_PROGRESS_SECONDS = 10.0
# Sparse INFO while a large Archive sync is healthy (#208) — not every heartbeat.
_HEAVY_FOLDER_INFO_PROGRESS_UID_STEP = 1000

# Monotonic id so request → arrive → index → list lines can be grepped together.
_heavy_pipeline_seq = 0


def _next_heavy_pipeline_id() -> str:
    global _heavy_pipeline_seq
    _heavy_pipeline_seq += 1
    return f"hf{_heavy_pipeline_seq}"


def _log_heavy_pipeline(
    stage: str,
    account_uid: str,
    folder_name: str,
    *,
    pipeline_id: str,
    level: int = logging.DEBUG,
    **fields: Any,
) -> None:
    """Structured Archive/Trash/Junk pipeline logs (#208).

    Default is DEBUG so normal runs do not flood ``post.log``. Callers pass
    ``level=logging.INFO`` for sparse milestones (request start, incomplete
    keep-alive). Stages:
      request — Post asked Camel/Graph for more headers (``refresh_info``)
      arrive  — Camel summary UID count grew (server data landed locally)
      index   — Post folder-index gained header rows
      list    — Gtk message list bind/append (see ``visible`` for UX expectation)
    """
    parts = [
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None
    ]
    suffix = (" " + " ".join(parts)) if parts else ""
    log.log(
        level,
        "Heavy-folder pipeline stage=%s id=%s %s/%s%s",
        stage,
        pipeline_id,
        account_uid,
        folder_name,
        suffix,
    )


def _folder_index_is_cacheable(index: _FolderMessageIndex) -> bool:
    if index.total <= 0:
        return True
    return len(index.messages) >= index.total


_INBOX_FOLDER_ALIASES = ("INBOX", "Inbox", "inbox")


def _inbox_folder_name_aliases(folder_name: str) -> tuple[str, ...]:
    """Camel M365 stores Inbox on disk; Post may open INBOX after a GOA failure."""
    if folder_name in _INBOX_FOLDER_ALIASES:
        return _INBOX_FOLDER_ALIASES
    return (folder_name,)


def _should_save_heavy_folder_index(
    messages: list[dict],
    existing: tuple[list[dict], int, int] | None,
) -> bool:
    """Grow-only: save incomplete heavy indexes when larger than disk (#208).

    Also allow a smaller list when it only collapses duplicate RestIds for the
    same logical messages (#267).
    """
    if existing is None:
        return bool(messages)
    if len(messages) > len(existing[0]):
        return True
    if len(messages) < len(existing[0]):
        return folder_index_covers_identities(messages, existing[0])
    return False


def _record_folder_index_uid_remap(
    uid_remaps: dict[str, str],
    replaced_uid: str | None,
    new_uid: str,
) -> None:
    if not replaced_uid or not new_uid or replaced_uid == new_uid:
        return
    for old_uid, current in list(uid_remaps.items()):
        if current == replaced_uid:
            uid_remaps[old_uid] = new_uid
    uid_remaps[replaced_uid] = new_uid


def _upsert_message_into_folder_index(
    by_uid: dict[str, dict],
    message: dict,
    *,
    prefer_uids: set[str] | None = None,
    uid_remaps: dict[str, str] | None = None,
    by_identity: dict[str, str] | None = None,
) -> str | None:
    """Identity-keyed upsert for heavy-folder ``by_uid`` (#267)."""
    replaced = upsert_folder_index_by_identity(
        by_uid,
        message,
        prefer_uids=prefer_uids,
        by_identity=by_identity,
    )
    if uid_remaps is not None:
        _record_folder_index_uid_remap(
            uid_remaps, replaced, str(message.get("uid") or "")
        )
    return replaced


def _heavy_folder_camel_behind_server(camel_uids: int, known_total: int) -> bool:
    """True when Camel summary is clearly short of server STATUS (#208).

    Interrupted M365 deltas leave Archive with thousands of local UIDs, an empty
    or stale delta link, and Graph ``totalItemCount`` still much larger. A full
    ``refresh_info`` then rewalks known headers with ``new=0`` for a long time
    before UID count grows again.
    """
    if known_total <= 0:
        return False
    return (known_total - max(0, camel_uids)) >= _HEAVY_FOLDER_INCOMPLETE_CAMEL_GAP


def _read_unflagged_uids(index: _FolderMessageIndex) -> list[str]:
    return [
        message["uid"]
        for message in index.messages
        if message.get("uid") and message_is_read_unflagged(message)
    ]


# Camel providers that authenticate via session OAuth (not IMAP password).
_OAUTH_SERVICE_MECHANISMS = frozenset({"XOAUTH2", "Microsoft365"})


AuthOutcomeCallback = Callable[[str, bool], None]


class MailSession(Camel.Session):
    """Camel session: OAuth via ESource, password auth for IMAP/SMTP."""

    __gtype_name__ = "PostMailSession"

    def __init__(
        self,
        registry: EDataServer.SourceRegistry,
        password_prompt: PasswordPromptCallback | None = None,
        on_auth_outcome: AuthOutcomeCallback | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._registry = registry
        self._password_prompt = password_prompt
        self._on_auth_outcome = on_auth_outcome

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback

    def set_auth_outcome_callback(
        self, callback: AuthOutcomeCallback | None
    ) -> None:
        self._on_auth_outcome = callback

    def do_get_filter_driver(self, type, for_folder=None):
        """Required when Camel parses MIME (e.g. reading messages)."""
        return Camel.FilterDriver.new(self)

    def _credential_source(self, service) -> EDataServer.Source | None:
        """Account ESource for a Camel service (matches Evolution's EMailSession)."""
        return self._registry.ref_source(service.get_uid())

    def _report_auth_outcome(self, service_uid: str, success: bool) -> None:
        if self._on_auth_outcome is not None:
            self._on_auth_outcome(service_uid, success)

    def do_authenticate_sync(self, service, mechanism=None, cancellable=None):
        """Password auth for IMAP/SMTP; OAuth for XOAUTH2 / Microsoft 365 providers."""
        service_uid = service.get_uid()
        if mechanism in _OAUTH_SERVICE_MECHANISMS:
            result = service.authenticate_sync(mechanism, cancellable)
            ok = result == Camel.AuthenticationResult.ACCEPTED
            self._report_auth_outcome(service_uid, ok)
            if ok:
                return True
            raise authentication_failed_error(
                f"Authentication failed for {service_uid}"
            )

        source = self._credential_source(service)
        if source is None:
            self._report_auth_outcome(service_uid, False)
            raise authentication_failed_error(
                f"No credential source for {service_uid}"
            )

        try:
            accepted = authenticate_service_sync(
                service,
                source,
                self._registry,
                mechanism,
                cancellable,
                self._password_prompt,
            )
        except GLib.Error:
            # Cancelled background loads must not be recorded as sign-in failure.
            if cancellable is not None and cancellable.is_cancelled():
                raise
            self._report_auth_outcome(service_uid, False)
            raise

        if accepted:
            self._report_auth_outcome(service_uid, True)
            return True

        log.warning(
            "Authentication failed for %s (mechanism=%s)",
            source.get_display_name(),
            mechanism,
        )
        self._report_auth_outcome(service_uid, False)
        # Camel requires a GError when authenticate_sync fails; returning False
        # alone leaves the store half-connected and skips our offline badge (#168).
        raise authentication_failed_error(
            f"Authentication failed for {source.get_display_name() or service_uid}"
        )

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
class FlushSendQueueResult:
    """Outcome of flushing the local Outbox queue."""

    sent: int = 0
    error_message: str | None = None
    failed_account_uid: str | None = None


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
    # Set when Graph RestId recovery fetches a live sibling UID (#294).
    _recovered_read_uid: str | None = field(default=None, init=False, repr=False)
    _correspondent_indexes: dict[str, list[Correspondent]] = field(
        default_factory=dict, init=False
    )
    _folder_tree_cache: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _network_available: bool = field(default=True, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _password_prompt: PasswordPromptCallback | None = field(default=None, init=False)
    _account_connect_health: dict[str, AccountConnectHealth] = field(
        default_factory=dict, init=False, repr=False
    )
    # In-flight / timed-out move state for sidebar badge + fail-fast (#189).
    _account_transfer_state: dict[str, AccountTransferState] = field(
        default_factory=dict, init=False, repr=False
    )
    _account_health_changed: Callable[[str], None] | None = field(
        default=None, init=False, repr=False
    )
    _pending_mail_ops: int = field(default=0, init=False)
    _pending_mail_ops_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )
    _outbound_sends_in_progress: int = field(default=0, init=False)
    _outbound_sends_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )
    # Archive/move/trash transfers submitted from the UI (#189 quit safety).
    _folder_transfers_in_progress: int = field(default=0, init=False)
    _folder_transfers_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )
    _active_outbound_deliveries: set[str] = field(default_factory=set, init=False)
    _flushing_operation_queue: bool = field(default=False, init=False)
    _flushing_draft_queue: bool = field(default=False, init=False)
    _offline_sync: OfflineBodySyncCoordinator | None = field(
        default=None, init=False, repr=False
    )
    _mail_io_callbacks_registered: bool = field(default=False, init=False, repr=False)
    _offline_body_sync_held: bool = field(default=False, init=False, repr=False)
    _folder_search_cancellable: Gio.Cancellable | None = field(
        default=None, init=False, repr=False
    )
    _folder_search_state_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _sync_setup_cancel: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )
    _sync_setup_resume: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )
    # Concurrent sidebar folder-tree loads (one per account at startup).
    # Preempt may cancel these so interactive mail work can run; registering
    # must not cancel siblings. Do not share with refresh ops — message loads
    # cancel refresh only so tree loads are not thrash-cancelled into
    # permanent "Loading folders…".
    _folder_list_cancellables: set[Gio.Cancellable] = field(
        default_factory=set, init=False, repr=False
    )
    # Folder-info REFRESH / refresh_info_sync for counts and index sync.
    # Cancelled by message loads and preempt without aborting sidebar tree loads.
    _folder_refresh_cancellables: set[Gio.Cancellable] = field(
        default_factory=set, init=False, repr=False
    )
    # Heavy-folder Graph refresh_info while Archive/etc. is open. Not cancelled
    # by sidebar count polls (cancel_folder_refresh) — only by explicit
    # cancel_heavy_folder_index_refresh when leaving the folder (#208).
    _heavy_index_refresh_cancellables: set[Gio.Cancellable] = field(
        default_factory=set, init=False, repr=False
    )
    # Survives load_id / cursor resets so we do not keep calling
    # prepare_content_refresh (which wipes M365 sync progress) (#208).
    _heavy_index_sessions: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
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
            self._on_background_resume,
        )
        self._mail_io_callbacks_registered = True

    def _on_background_resume(self) -> None:
        self.schedule_offline_body_sync()
        if self._sync_setup_resume is not None:
            self._sync_setup_resume()

    def _preempt_background_work(self) -> None:
        self.cancel_folder_search()
        self.cancel_folder_refresh()
        # Cancel in-flight sidebar tree loads so interactive work is not stuck
        # behind a slow M365 get_folder_info_sync; list_folders falls back to
        # cache when available.
        self.cancel_folder_list()
        self.offline_sync.cancel_all()
        if self._sync_setup_cancel is not None:
            self._sync_setup_cancel()

    def cancel_folder_list(self) -> None:
        with self._folder_list_state_lock:
            cancellables = list(self._folder_list_cancellables)
            self._folder_list_cancellables.clear()
        if cancellables:
            search_trace("folder_list_cancel", count=len(cancellables))
            for cancellable in cancellables:
                cancellable.cancel()

    def cancel_folder_refresh(self) -> None:
        """Abort folder-info REFRESH / refresh_info_sync (not sidebar tree loads).

        Does not cancel an in-flight heavy-folder Archive refresh — that would
        freeze header growth at the local summary size (#208).
        """
        with self._folder_list_state_lock:
            cancellables = list(self._folder_refresh_cancellables)
            self._folder_refresh_cancellables.clear()
        if cancellables:
            search_trace("folder_refresh_cancel", count=len(cancellables))
            for cancellable in cancellables:
                cancellable.cancel()

    def cancel_heavy_folder_index_refresh(self) -> None:
        """Abort heavy-folder Graph refresh when leaving Archive/Trash/Junk."""
        with self._folder_list_state_lock:
            cancellables = list(self._heavy_index_refresh_cancellables)
            self._heavy_index_refresh_cancellables.clear()
        for cancellable in cancellables:
            cancellable.cancel()

    def clear_heavy_folder_index_session(
        self, account_uid: str, folder_name: str
    ) -> None:
        """Drop per-folder prepare/seed flags when navigating away (#208)."""
        self._heavy_index_sessions.pop((account_uid, folder_name), None)

    def _heavy_index_session(self, account_uid: str, folder_name: str) -> dict[str, Any]:
        key = (account_uid, folder_name)
        session = self._heavy_index_sessions.get(key)
        if session is None:
            session = {
                "did_prepare_content_refresh": False,
                "status_seeded": False,
                "did_server_uid_search": False,
            }
            self._heavy_index_sessions[key] = session
        return session

    def _register_heavy_index_refresh_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._heavy_index_refresh_cancellables.add(cancellable)

    def _unregister_heavy_index_refresh_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._heavy_index_refresh_cancellables.discard(cancellable)

    def _register_folder_list_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._folder_list_cancellables.add(cancellable)

    def _unregister_folder_list_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._folder_list_cancellables.discard(cancellable)

    def _register_folder_refresh_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._folder_refresh_cancellables.add(cancellable)

    def _unregister_folder_refresh_cancellable(
        self, cancellable: Gio.Cancellable
    ) -> None:
        with self._folder_list_state_lock:
            self._folder_refresh_cancellables.discard(cancellable)

    def set_sync_setup_cancel_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        self._sync_setup_cancel = callback

    def set_sync_setup_resume_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        self._sync_setup_resume = callback
        if self._mail_io_callbacks_registered:
            get_mail_io_thread().set_background_preempt_callbacks(
                self._preempt_background_work,
                self._on_background_resume,
            )

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

    def hold_offline_body_sync(self, hold: bool) -> None:
        """Pause or resume background offline body caching for interactive UI.

        While held, folder opens / Archive indexing keep the mail I/O thread;
        other accounts' offline backfill must not resume until released.
        """
        self._offline_body_sync_held = bool(hold)
        if hold:
            self.offline_sync.cancel_all()
        else:
            self.schedule_offline_body_sync()

    def offline_body_sync_is_held(self) -> bool:
        """True while folder opens / heavy-folder indexing owns mail I/O (#208)."""
        return self._offline_body_sync_held

    def schedule_offline_body_sync(self, account_uid: str | None = None) -> None:
        if self._offline_body_sync_held:
            return
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

    def begin_folder_transfer(self) -> None:
        """Mark an Archive/move/trash worker as in flight (call before submit)."""
        with self._folder_transfers_cond:
            self._folder_transfers_in_progress += 1

    def end_folder_transfer(self) -> None:
        with self._folder_transfers_cond:
            if self._folder_transfers_in_progress > 0:
                self._folder_transfers_in_progress -= 1
            if self._folder_transfers_in_progress == 0:
                self._folder_transfers_cond.notify_all()

    def folder_transfers_pending(self) -> bool:
        with self._folder_transfers_cond:
            return self._folder_transfers_in_progress > 0

    def wait_for_folder_transfers(
        self,
        timeout: float = _TRANSFER_TIMEOUT_SECONDS + 15.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        with self._folder_transfers_cond:
            while self._folder_transfers_in_progress > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "Timed out waiting for %d folder transfer(s)",
                        self._folder_transfers_in_progress,
                    )
                    return False
                self._folder_transfers_cond.wait(timeout=remaining)
        return True

    def _reset_folder_transfer_counter_after_timeout(self) -> None:
        with self._folder_transfers_cond:
            if self._folder_transfers_in_progress <= 0:
                return
            log.warning(
                "Resetting stuck folder transfer counter (%d) after wait timeout",
                self._folder_transfers_in_progress,
            )
            self._folder_transfers_in_progress = 0
            self._folder_transfers_cond.notify_all()

    def when_folder_transfers_complete(
        self,
        callback: Callable[[], None],
        *,
        timeout: float = _TRANSFER_TIMEOUT_SECONDS + 15.0,
    ) -> None:
        """Run callback on the GTK thread after Archive/move/trash workers finish."""

        def worker() -> None:
            completed = self.wait_for_folder_transfers(timeout=timeout)
            if not completed:
                self._reset_folder_transfer_counter_after_timeout()
            GLib.idle_add(_run_on_gtk_thread, callback)

        threading.Thread(
            target=worker, daemon=True, name="post-transfer-wait"
        ).start()

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
        # Finish in-flight Archive/move/trash before tearing down Camel (#189).
        self.wait_for_folder_transfers()
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
        service._drop_orphan_account_caches()
        return service

    def set_password_prompt(self, callback: PasswordPromptCallback | None) -> None:
        self._password_prompt = callback
        if isinstance(self._session, MailSession):
            self._session.set_password_prompt(callback)

    def set_account_health_changed_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        """UI callback invoked on the GTK idle loop when connect health changes."""
        self._account_health_changed = callback

    def get_account_connect_health(self, account_uid: str) -> AccountConnectHealth:
        with self._lock:
            return self._account_connect_health.get(account_uid, "ok")

    def get_account_transfer_state(self, account_uid: str) -> AccountTransferState:
        with self._lock:
            return self._account_transfer_state.get(account_uid, "idle")

    def set_account_transfer_state(
        self, account_uid: str, state: AccountTransferState
    ) -> None:
        """Update move/busy badge state and notify the sidebar (#189)."""
        notify = False
        with self._lock:
            current = self._account_transfer_state.get(account_uid, "idle")
            if state == "idle":
                if account_uid in self._account_transfer_state:
                    del self._account_transfer_state[account_uid]
                    notify = True
            elif current != state:
                self._account_transfer_state[account_uid] = state
                notify = True
        if notify:
            self._notify_account_health_changed(account_uid)

    def account_uses_goa(self, account_uid: str) -> bool:
        """Return True when this account authenticates via GNOME Online Accounts."""
        source = self.registry.ref_source(account_uid)
        if source is None:
            return False
        return source_uses_goa(self.registry, source)

    def open_online_accounts_settings(self) -> bool:
        """Open GNOME Settings → Online Accounts for re-authentication."""
        return open_gnome_online_accounts()

    def set_account_connect_health(
        self, account_uid: str, health: AccountConnectHealth
    ) -> None:
        notify = False
        with self._lock:
            current = self._account_connect_health.get(account_uid, "ok")
            if health == "ok":
                if account_uid in self._account_connect_health:
                    del self._account_connect_health[account_uid]
                    notify = True
            else:
                if current != health:
                    self._account_connect_health[account_uid] = health
                # Always refresh UI for degraded state (sidebar icons may be rebuilt).
                notify = True
        if notify:
            self._notify_account_health_changed(account_uid)

    def _notify_account_health_changed(self, account_uid: str) -> None:
        callback = self._account_health_changed
        if callback is None:
            return
        GLib.idle_add(callback, account_uid)

    def _account_uid_for_service(self, service_uid: str) -> str | None:
        if not service_uid:
            return None
        with self._lock:
            if service_uid in self._accounts_by_uid:
                return service_uid
            for account in self._accounts_by_uid.values():
                if account.transport_uid == service_uid:
                    return account.uid

        source = self.registry.ref_source(service_uid)
        if source is not None:
            if source.has_extension("Mail Account"):
                return service_uid
            if source.has_extension("Mail Transport"):
                parent_uid = source.get_parent()
                name = source.get_display_name()
                try:
                    for account in self.list_accounts():
                        if account.transport_uid == service_uid:
                            return account.uid
                        if name and account.name == name:
                            return account.uid
                        if parent_uid:
                            account_source = self.registry.ref_source(account.uid)
                            if (
                                account_source is not None
                                and account_source.get_parent() == parent_uid
                            ):
                                return account.uid
                except Exception:
                    log.debug(
                        "Could not resolve transport %s to account",
                        service_uid,
                        exc_info=True,
                    )
        try:
            for account in self.list_accounts():
                if account.uid == service_uid or account.transport_uid == service_uid:
                    return account.uid
        except Exception:
            log.debug(
                "Could not resolve account for service %s", service_uid, exc_info=True
            )
        return None

    def resolve_account_uid_for_service(self, service_uid: str) -> str | None:
        """Map a Camel store/transport UID to its mail account UID."""
        return self._account_uid_for_service(service_uid)

    def _on_service_auth_outcome(self, service_uid: str, success: bool) -> None:
        account_uid = self._account_uid_for_service(service_uid)
        if account_uid is None:
            log.warning(
                "Auth outcome for unknown service %s (success=%s)",
                service_uid,
                success,
            )
            return
        if success:
            self.set_account_connect_health(account_uid, "ok")
        else:
            self.set_account_connect_health(account_uid, "needs_sign_in")

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
        """Persist and apply per-account user online/offline state.

        Preference is saved immediately. Camel ``set_online_sync`` runs on the
        mail I/O thread without blocking the GTK main loop (Take Offline from a
        context menu must stay responsive).
        """
        from post.preferences import set_account_user_online as save_pref

        save_pref(account_uid, online)
        if is_mail_io_thread():
            self._apply_account_user_online_unlocked(account_uid)
            return
        get_mail_io_thread().submit_front(
            self._apply_account_user_online_unlocked, account_uid
        )

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
        self._drop_orphan_account_caches()

    def _mail_account_uids_from_registry(self) -> set[str] | None:
        """Mail Account source uids (enabled and disabled). None if listing failed."""
        try:
            sources = self.registry.list_sources("Mail Account")
        except Exception:
            log.exception("Could not list Mail Account sources for cache GC")
            return None
        if sources is None:
            return None
        live: set[str] = set()
        for source in sources:
            uid = source.get_uid()
            if uid:
                live.add(uid)
        return live

    def _drop_orphan_account_caches(self) -> None:
        """Delete Post caches whose EDS Mail Account source is gone (#366)."""
        live = self._mail_account_uids_from_registry()
        if live is None:
            return
        drop_orphan_account_caches(live)

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
            on_auth_outcome=self._on_service_auth_outcome,
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
        self._store_folder_index(
            account_uid,
            folder_name,
            _FolderMessageIndex(
                messages=list(messages),
                unread=unread,
                total=total,
            ),
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

    def get_folder_index_for_search(
        self,
        account_uid: str,
        folder_name: str,
    ) -> tuple[list[dict], int, int] | None:
        """Return RAM ∪ disk folder-index rows for search (#363).

        Does not replace the in-memory list index.
        """
        prepared = self._folder_index_for_search_unlocked(
            account_uid, folder_name
        )
        if prepared is None:
            return None
        messages, unread, total, _source = prepared
        return messages, unread, total

    def _folder_index_for_search_unlocked(
        self,
        account_uid: str,
        folder_name: str,
    ) -> tuple[list[dict], int, int, FolderIndexSource] | None:
        memory_messages: list[dict] | None = None
        memory_unread = 0
        memory_total = 0
        with self._lock:
            index = self._folder_indexes.get((account_uid, folder_name))
            if index is not None:
                memory_messages = list(index.messages)
                memory_unread = index.unread
                memory_total = index.total
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is None:
            if memory_messages is None:
                return None
            return memory_messages, memory_unread, memory_total, "memory"
        disk_messages, disk_unread, disk_total = cached
        if not memory_messages:
            return list(disk_messages), disk_unread, disk_total, "disk_cache"
        unioned = union_folder_index_messages(memory_messages, disk_messages)
        if len(unioned) != len(memory_messages):
            search_trace(
                "search_index_union_disk",
                account=account_uid,
                folder=folder_name,
                memory=len(memory_messages),
                disk=len(disk_messages),
                union=len(unioned),
            )
        unread = max(memory_unread, disk_unread)
        total = max(memory_total, disk_total, len(unioned))
        source: FolderIndexSource = (
            "disk_cache" if len(unioned) > len(memory_messages) else "memory"
        )
        return unioned, unread, total, source

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
        correspondent_cache.invalidate(account_uid)

    def invalidate_account_connection(self, account_uid: str) -> None:
        """Drop cached Camel store/transport so the next open uses fresh credentials."""
        run_on_mail_thread(self._invalidate_account_connection_unlocked, account_uid)

    def _invalidate_account_connection_unlocked(self, account_uid: str) -> None:
        session: Camel.Session | None
        transport_uid: str | None = None
        with self._lock:
            self._stores.pop(account_uid, None)
            account = self._accounts_by_uid.get(account_uid)
            if account is not None:
                transport_uid = account.transport_uid
            if not transport_uid:
                try:
                    looked_up = self.get_account(account_uid)
                    transport_uid = looked_up.transport_uid
                except Exception:
                    transport_uid = None
            if transport_uid:
                self._transports.pop(transport_uid, None)
            self._folder_tree_cache.pop(account_uid, None)
            for key in list(self._folder_indexes):
                if key[0] == account_uid:
                    self._folder_indexes.pop(key, None)
            self._correspondent_indexes.pop(account_uid, None)
            session = self._session

        if session is None:
            return
        for service_uid in (account_uid, transport_uid):
            if not service_uid:
                continue
            try:
                service = session.ref_service(service_uid)
            except Exception:
                service = None
            if service is None:
                continue
            try:
                session.remove_service(service)
            except Exception:
                log.debug(
                    "Could not remove Camel service %s after credential change",
                    service_uid,
                    exc_info=True,
                )

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

    def guess_inbox_from_folder_index(self, account_uid: str) -> str | None:
        """Best Inbox name from persisted folder-index files (no Camel)."""
        names = folder_index_cache.cached_folder_names(account_uid)
        if not names:
            return None
        return guess_inbox_name(
            [{"full_name": name, "display_name": name} for name in names]
        )

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
        allow_online: bool = True,
    ) -> Camel.Store:
        if account_uid in self._stores:
            store = self._stores[account_uid]
            if not allow_online:
                if (
                    store.get_connection_status()
                    != Camel.ServiceConnectionStatus.CONNECTED
                ):
                    self._call_without_service_lock(
                        self._apply_local_only_store_state_unlocked,
                        store,
                        account_uid,
                        cancellable=cancellable,
                    )
                else:
                    self._configure_store_settings_unlocked(store, account_uid)
                return store
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

        if allow_online:
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

        if allow_online:
            self._call_without_service_lock(
                self._sync_store_online_state_unlocked,
                store,
                account_uid,
                cancellable=cancellable,
            )
        else:
            self._call_without_service_lock(
                self._apply_local_only_store_state_unlocked,
                store,
                account_uid,
                cancellable=cancellable,
            )

        self._configure_store_settings_unlocked(store, account_uid)
        return store

    def _apply_local_only_store_state_unlocked(
        self,
        store: Camel.Store,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Load Camel's on-disk summary without contacting the server."""
        if not isinstance(store, Camel.OfflineStore):
            return
        try:
            store.set_online_sync(False, cancellable)
        except GLib.Error:
            log.debug(
                "Could not set store offline for local cache (%s)",
                account_uid,
                exc_info=True,
            )

    def _sync_store_online_state_unlocked(
        self,
        store: Camel.Store,
        account_uid: str,
        *,
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Connect / set online. Must not run while ``_lock`` is held."""
        effective = self._network_available and get_account_user_online(account_uid)
        try:
            if isinstance(store, Camel.OfflineStore):
                store.set_online_sync(effective, cancellable)
            elif effective:
                store.connect_sync(cancellable)
        except GLib.Error as exc:
            log.debug(
                "Store connect/online failed for %s", account_uid, exc_info=True
            )
            if effective:
                if is_sign_in_required_error(exc):
                    self.set_account_connect_health(account_uid, "needs_sign_in")
                elif self.get_account_connect_health(account_uid) != "needs_sign_in":
                    self.set_account_connect_health(account_uid, "not_connected")
            raise
        if effective:
            # Auth failures already set needs_sign_in via MailSession; keep that
            # even when OfflineStore stays usable for cached folders.
            if self.get_account_connect_health(account_uid) != "needs_sign_in":
                self.set_account_connect_health(account_uid, "ok")
        elif not get_account_user_online(account_uid):
            # Intentional offline — clear runtime degraded so Take Offline wins.
            self.set_account_connect_health(account_uid, "ok")

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
            try:
                if hasattr(Camel, "OfflineTransport") and isinstance(
                    transport, Camel.OfflineTransport
                ):
                    transport.set_online_sync(True, cancellable)
                else:
                    transport.connect_sync(cancellable)
            except GLib.Error:
                log.debug(
                    "Transport connect failed for account %s",
                    account_uid,
                    exc_info=True,
                )
                if self.get_account_connect_health(account_uid) != "needs_sign_in":
                    self.set_account_connect_health(account_uid, "not_connected")
                raise
            if self.get_account_connect_health(account_uid) != "needs_sign_in":
                self.set_account_connect_health(account_uid, "ok")

        self._call_without_service_lock(_connect_transport)
        return transport

    def flush_send_queue(self, *, force: bool = False) -> FlushSendQueueResult:
        """Try to send queued outbox messages."""
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

    def _flush_send_queue_unlocked(self, *, force: bool = False) -> FlushSendQueueResult:
        sent = 0
        error_message: str | None = None
        failed_account_uid: str | None = None
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
                    error_message = exc.user_message
                    failed_account_uid = queued.account_uid
                    break
                except Exception as exc:
                    log.exception("Failed to send queued message %s", queue_id)
                    error_message = user_send_error_message(exc)
                    failed_account_uid = queued.account_uid
                    break
                else:
                    sent += 1
            finally:
                self._end_outbound_send()
        return FlushSendQueueResult(
            sent=sent,
            error_message=error_message,
            failed_account_uid=failed_account_uid,
        )

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

    def get_correspondents_cached(self, account_uid: str) -> list[Correspondent]:
        """Return in-memory correspondents without joining the mail thread."""
        with self._lock:
            cached = self._correspondent_indexes.get(account_uid)
            if cached is None:
                return []
            return list(cached)

    def _get_correspondents_unlocked(self, account_uid: str) -> list[Correspondent]:
        with self._lock:
            cached = self._correspondent_indexes.get(account_uid)
            if cached is not None:
                return list(cached)
        loaded = correspondent_cache.load(account_uid)
        if loaded:
            self._remember_correspondents(account_uid, loaded, persist=False)
            return list(loaded)
        correspondents = self._build_correspondents_index_unlocked(account_uid)
        if correspondents:
            self._remember_correspondents(account_uid, correspondents, persist=True)
        return correspondents

    def _store_folder_index(
        self,
        account_uid: str,
        folder_name: str,
        index: _FolderMessageIndex,
    ) -> None:
        """Install a folder index and merge correspondents (#313)."""
        with self._lock:
            self._folder_indexes[(account_uid, folder_name)] = index
        self._merge_correspondents_from_folder(
            account_uid, folder_name, index.messages
        )

    def _remember_correspondents(
        self,
        account_uid: str,
        correspondents: list[Correspondent],
        *,
        persist: bool,
    ) -> None:
        if not correspondents:
            return
        with self._lock:
            self._correspondent_indexes[account_uid] = correspondents
        if persist:
            correspondent_cache.save(account_uid, correspondents)

    def _merge_correspondents_from_folder(
        self,
        account_uid: str,
        folder_name: str,
        messages: list[dict],
    ) -> None:
        if not folder_feeds_correspondents(folder_name) or not messages:
            return
        incoming = collect_correspondents(messages)
        if not incoming:
            return
        with self._lock:
            existing = self._correspondent_indexes.get(account_uid)
        if existing is None:
            existing = correspondent_cache.load(account_uid)
        if existing is None:
            # Not bootstrapped yet — next get_correspondents() harvests all
            # folder indexes. Do not persist a single-folder partial.
            return
        merged = merge_correspondents(existing, incoming)
        self._remember_correspondents(account_uid, merged, persist=True)

    def _correspondent_folder_names_unlocked(self, account_uid: str) -> list[str]:
        """Folder names that may feed autocomplete (cached tree / indexes only)."""
        names: set[str] = set()
        with self._lock:
            for folder in self._folder_tree_cache.get(account_uid) or []:
                full_name = folder.get("full_name")
                if full_name:
                    names.add(full_name)
            for key_account, folder_name in self._folder_indexes:
                if key_account == account_uid:
                    names.add(folder_name)
        for folder_name in folder_index_cache.cached_folder_names(account_uid):
            names.add(folder_name)
        return [name for name in names if folder_feeds_correspondents(name)]

    def _messages_for_correspondent_folder(
        self, account_uid: str, folder_name: str
    ) -> list[dict]:
        with self._lock:
            index = self._folder_indexes.get((account_uid, folder_name))
        if index is not None:
            return list(index.messages)
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is None:
            return []
        return list(cached[0])

    def _build_correspondents_index_unlocked(
        self, account_uid: str
    ) -> list[Correspondent]:
        """Build autocomplete from in-memory / disk folder indexes only (#156, #240, #313)."""
        loaded = correspondent_cache.load(account_uid)
        messages: list[dict] = []
        for folder_name in self._correspondent_folder_names_unlocked(account_uid):
            messages.extend(
                self._messages_for_correspondent_folder(account_uid, folder_name)
            )
        harvested = collect_correspondents(messages)
        if loaded is None:
            return harvested
        return merge_correspondents(loaded, harvested)

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
        store = self._get_store_unlocked(account_uid, allow_online=False)

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
            with self._lock:
                cached_early = self._folder_tree_cache.get(account_uid)
            if cached_early is not None:
                search_trace(
                    "folder_list_cancelled_using_cache", account=account_uid
                )
                return list(cached_early)
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
                    self._folder_tree_cache[account_uid] = _merge_heavy_folder_status_into_tree(account_uid, result)
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
                if cached is not None:
                    search_trace(
                        "folder_list_cancelled_using_cache", account=account_uid
                    )
                    return list(cached)
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
                        self._folder_tree_cache[account_uid] = _merge_heavy_folder_status_into_tree(account_uid, result)
                    return result
                raise RuntimeError(
                    "Could not list folders for this account"
                )
            folders: list[dict] = []
            walk_folder_info(root, folders)
            result = [f for f in folders if f.get("full_name")]
            result = _merge_heavy_folder_status_into_tree(account_uid, result)
            with self._lock:
                self._folder_tree_cache[account_uid] = _merge_heavy_folder_status_into_tree(account_uid, result)
            return result
        except GLib.Error as exc:
            if cancellable is not None and exc.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
            ):
                search_trace("folder_list_cancelled", account=account_uid)
                if cached is not None:
                    search_trace(
                        "folder_list_cancelled_using_cache", account=account_uid
                    )
                    return list(cached)
                raise
            if cached is not None and (
                is_network_unavailable_error(exc) or is_sign_in_required_error(exc)
            ):
                if is_sign_in_required_error(exc):
                    self.set_account_connect_health(account_uid, "needs_sign_in")
                log.debug(
                    "Using cached folder list for account %s after %s",
                    account_uid,
                    "sign-in error" if is_sign_in_required_error(exc) else "offline",
                )
                return cached
            if is_network_unavailable_error(exc) or is_sign_in_required_error(exc):
                if is_sign_in_required_error(exc):
                    self.set_account_connect_health(account_uid, "needs_sign_in")
                result = self._list_folders_from_local_store_unlocked(account_uid)
                if result:
                    with self._lock:
                        self._folder_tree_cache[account_uid] = _merge_heavy_folder_status_into_tree(account_uid, result)
                    return result
            raise

    def get_folder_stats(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        """Return live (unread, total) counts by opening the folder."""
        return run_on_mail_thread(
            self._get_folder_stats_unlocked, account_uid, folder_name
        )

    def get_account_folder_stats(
        self, account_uid: str
    ) -> dict[str, tuple[int, int]]:
        """Return unread/total for all folders via store folder-info REFRESH.

        Uses IMAP STATUS-style folder info instead of opening each Camel.Folder
        (avoids Folder::changed storms and heavy per-folder refresh_info_sync).
        """
        return run_on_mail_thread(
            self._get_account_folder_stats_unlocked, account_uid
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
        unread = folder_get_unread_count(folder)
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
        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
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
        result["archived_count"] = len(result.get("moved_uids") or [])
        return result

    def _count_read_unflagged_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> int:
        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
        return len(_read_unflagged_uids(index))

    def _archive_read_unflagged_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
        uids = _read_unflagged_uids(index)
        if not uids:
            return {
                "archived_count": 0,
                "source_folder_unread": index.unread,
                "source_folder_total": index.total,
            }
        result = self._archive_messages_unlocked(account_uid, folder_name, uids)
        result["archived_count"] = len(result.get("moved_uids") or [])
        return result

    def _archive_all_messages_unlocked(
        self, account_uid: str, folder_name: str
    ) -> dict[str, Any]:
        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
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
        result["archived_count"] = len(result.get("moved_uids") or [])
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
        correspondent_cache.invalidate(account_uid)

    def _cached_folder_stats_unlocked(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int] | None:
        """STATUS-style counts from the folder tree cache (not folder-index)."""
        for folder in self._folder_tree_cache.get(account_uid) or []:
            if folder.get("full_name") == folder_name:
                unread = int(folder.get("unread", -1))
                total = int(folder.get("total", -1))
                if is_heavy_folder_name(folder_name):
                    return folder_status_cache.resolve_sidebar(
                        account_uid, folder_name, unread, total
                    )
                return unread, total
        if is_heavy_folder_name(folder_name):
            cached = folder_status_cache.load(account_uid, folder_name)
            if cached is not None:
                return cached
            return -1, -1
        return None

    def _folder_index_stats_unlocked(
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

    def get_folder_status_totals(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int] | None:
        """Return sidebar STATUS unread/total (high-water for heavy folders)."""
        with self._lock:
            return self._cached_folder_stats_unlocked(account_uid, folder_name)

    def _seed_heavy_folder_status_from_graph_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        local_indexed: int = 0,
    ) -> None:
        """Lock in Graph totalItemCount as STATUS for M365 heavy folders (#208)."""
        try:
            account = self.get_account(account_uid)
        except Exception:
            return
        if (account.backend or "").lower() != "microsoft365":
            return
        if folder_status_cache.load(account_uid, folder_name) is not None:
            return
        try:
            store = self._get_store_unlocked(account_uid)
            session = self._ensure_session()
        except Exception:
            log.debug(
                "Graph STATUS seed: no store/session for %s",
                account_uid,
                exc_info=True,
            )
            return
        cancellable = Gio.Cancellable()
        try:
            ok, token, _expires = session.get_oauth2_access_token_sync(
                store, cancellable
            )
        except Exception:
            log.debug(
                "Graph STATUS seed: OAuth token failed for %s",
                account_uid,
                exc_info=True,
            )
            return
        if not ok or not token:
            return
        counts = graph_folder_counts.fetch_mail_folder_counts(
            token, folder_name, cancellable=cancellable
        )
        if counts is None:
            log.info(
                "Graph STATUS seed: no counts for %s/%s",
                account_uid,
                folder_name,
            )
            return
        unread, total = counts
        folder_status_cache.observe(
            account_uid,
            folder_name,
            unread,
            total,
            trusted=True,
            local_indexed=local_indexed,
        )
        loaded = folder_status_cache.load(account_uid, folder_name)
        if loaded is not None:
            log.info(
                "Graph STATUS seed for %s/%s: unread=%d total=%d",
                account_uid,
                folder_name,
                loaded[0],
                loaded[1],
            )
            # Keep sidebar tree cache in sync with the real server total.
            with self._lock:
                cached = self._folder_tree_cache.get(account_uid)
                if cached is not None:
                    self._folder_tree_cache[account_uid] = (
                        _merge_heavy_folder_status_into_tree(account_uid, cached)
                    )

    def _get_folder_stats_unlocked(
        self, account_uid: str, folder_name: str
    ) -> tuple[int, int]:
        # Heavy folders: never use Camel summary / folder-index as STATUS (#208).
        # Partial local summaries (hundreds) were overwriting real server totals
        # (tens of thousands) in the sidebar.
        if is_heavy_folder_name(folder_name):
            with self._lock:
                status = self._cached_folder_stats_unlocked(
                    account_uid, folder_name
                )
            if status is not None and status[1] >= 0:
                return status
            # Store FolderInfo REFRESH — STATUS-style, not open-folder summary.
            stats = self._get_account_folder_stats_unlocked(account_uid)
            if folder_name in stats:
                return stats[folder_name]
            return (-1, -1)

        # Never hold MailService._lock across Camel network I/O (#210): GTK and
        # other mail-thread helpers that need the lock would freeze for the
        # full refresh_info_sync duration (or until cancel is honored).
        with self._lock:
            store = self._get_store_unlocked(account_uid)
            if not self._network_available:
                cached = self._cached_folder_stats_unlocked(
                    account_uid, folder_name
                )
                if cached is not None:
                    return cached
                indexed = self._folder_index_stats_unlocked(
                    account_uid, folder_name
                )
                if indexed is not None:
                    return indexed

        cancellable = Gio.Cancellable()
        self._register_folder_refresh_cancellable(cancellable)
        timer = threading.Timer(
            _FOLDER_STATS_TIMEOUT_SECONDS, cancellable.cancel
        )
        timer.start()
        try:
            if cancellable.is_cancelled():
                with self._lock:
                    cached = self._cached_folder_stats_unlocked(
                        account_uid, folder_name
                    )
                if cached is not None:
                    return cached
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(),
                    "Operation was cancelled",
                    Gio.IOErrorEnum.CANCELLED,
                )
            folder = store.get_folder_sync(folder_name, 0, cancellable)
            if folder is None:
                raise ValueError(f"Folder not found: {folder_name}")
            try:
                folder.refresh_info_sync(cancellable)
            except GLib.Error as exc:
                if exc.matches(
                    Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
                ) or is_network_unavailable_error(exc):
                    with self._lock:
                        cached = self._cached_folder_stats_unlocked(
                            account_uid, folder_name
                        )
                        if cached is None:
                            cached = self._folder_index_stats_unlocked(
                                account_uid, folder_name
                            )
                    if cached is not None:
                        return cached
                    if exc.matches(
                        Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
                    ):
                        return (-1, -1)
                raise
            return folder_get_unread_count(folder), folder.get_message_count()
        finally:
            timer.cancel()
            self._unregister_folder_refresh_cancellable(cancellable)

    def _get_account_folder_stats_unlocked(
        self, account_uid: str
    ) -> dict[str, tuple[int, int]]:
        def _stats_from_cached_tree() -> dict[str, tuple[int, int]]:
            with self._lock:
                cached = self._folder_tree_cache.get(account_uid) or []
            out: dict[str, tuple[int, int]] = {}
            for folder in cached:
                name = folder.get("full_name")
                if not name:
                    continue
                unread = int(folder.get("unread", -1))
                total = int(folder.get("total", -1))
                if is_heavy_folder_name(name):
                    out[name] = folder_status_cache.resolve_sidebar(
                        account_uid, name, unread, total
                    )
                else:
                    out[name] = (unread, total)
            return out

        with self._lock:
            store = self._get_store_unlocked(account_uid)
        if not self._network_available:
            return _stats_from_cached_tree()
        flags = (
            Camel.StoreGetFolderInfoFlags.RECURSIVE
            | Camel.StoreGetFolderInfoFlags.REFRESH
        )
        cancellable = Gio.Cancellable()
        self._register_folder_refresh_cancellable(cancellable)
        # Bound store FolderInfo REFRESH the same way as per-folder stats (#210).
        # Heavy-folder get_folder_stats falls into this path on cache miss.
        timer = threading.Timer(
            _FOLDER_STATS_TIMEOUT_SECONDS, cancellable.cancel
        )
        timer.start()
        try:
            if cancellable.is_cancelled():
                return _stats_from_cached_tree()
            root = store.get_folder_info_sync(None, flags, cancellable)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return _stats_from_cached_tree()
            if is_network_unavailable_error(exc):
                return _stats_from_cached_tree()
            raise
        finally:
            timer.cancel()
            self._unregister_folder_refresh_cancellable(cancellable)
        # Camel M365 can fail without setting GError (#156); keep prior counts.
        if root is None:
            log.warning(
                "Folder stats refresh failed for account %s without GError; "
                "keeping prior cache if any",
                account_uid,
            )
            return _stats_from_cached_tree()
        folders: list[dict] = []
        walk_folder_info(root, folders)
        stats: dict[str, tuple[int, int]] = {}
        for folder in folders:
            full_name = folder.get("full_name")
            if not full_name:
                continue
            stats[full_name] = (
                int(folder.get("unread", -1)),
                int(folder.get("total", -1)),
            )
        stats = _apply_heavy_status_high_water(account_uid, stats)
        # Keep tree cache in sync with STATUS high-water for heavy folders.
        with self._lock:
            cached = self._folder_tree_cache.get(account_uid)
            if cached is not None:
                self._folder_tree_cache[account_uid] = (
                    _merge_heavy_folder_status_into_tree(account_uid, cached)
                )
        return stats

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
        ordered = priority + remaining
        for name in folder_index_cache.cached_folder_names(account_uid):
            if not name or name in seen:
                continue
            if is_post_outbox_folder(name) or is_virtual_folder(name):
                continue
            seen.add(name)
            ordered.append({"full_name": name, "display_name": name})
        return ordered

    def _prepare_single_folder_search_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
    ) -> tuple[list[dict], int, FolderIndexSource, Any | None, bool]:
        prepared = self._folder_index_for_search_unlocked(
            account_uid, folder_name
        )
        if prepared is not None:
            messages, unread, _total, source = prepared
        else:
            search_trace(
                "search_index_load",
                account=account_uid,
                folder=folder_name,
                source="missing_memory",
            )
            with self._lock:
                index, source = self._get_folder_index_unlocked(
                    account_uid, folder_name, sync=False
                )
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

    def _prefer_nonempty_folder_index(
        self,
        account_uid: str,
        folder_name: str,
        index: _FolderMessageIndex,
    ) -> tuple[_FolderMessageIndex, FolderIndexSource | None]:
        """Keep a larger RAM/disk index when Camel's summary came back empty."""
        if index.messages:
            return index, None
        key = (account_uid, folder_name)
        ram = self._folder_indexes.get(key)
        if ram is not None and ram.messages:
            log.warning(
                "Keeping in-memory folder index for %s/%s after empty Camel summary "
                "(memory=%d)",
                account_uid,
                folder_name,
                len(ram.messages),
            )
            return ram, "memory"
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is not None and cached[0]:
            messages, unread, total = cached
            log.warning(
                "Keeping on-disk folder index for %s/%s after empty Camel summary "
                "(disk=%d)",
                account_uid,
                folder_name,
                len(messages),
            )
            return (
                _FolderMessageIndex(
                    messages=messages, unread=unread, total=total
                ),
                "disk_cache",
            )
        return index, None

    def _get_folder_index_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        sync: bool,
    ) -> tuple[_FolderMessageIndex, FolderIndexSource]:
        key = (account_uid, folder_name)
        heavy = is_heavy_folder_name(folder_name)
        if sync and heavy:
            # Archive/Trash/Junk are grown by continue_heavy_folder_index.
            # A full Camel refresh_info rebuild here collapses a multi-thousand
            # progressive index back to the local summary (~1.3k) and wipes
            # disk (#208). Prefer memory/disk; seed from local summary only
            # when nothing is cached yet.
            existing = self._folder_indexes.get(key)
            source: FolderIndexSource = "memory"
            if existing is None:
                cached = folder_index_cache.load(account_uid, folder_name)
                if cached is not None:
                    messages, unread, total = cached
                    existing = _FolderMessageIndex(
                        messages=messages,
                        unread=unread,
                        total=total,
                    )
                    self._store_folder_index(account_uid, folder_name, existing)
                    source = "disk_cache"
            if existing is not None:
                existing, source = self._union_heavy_index_with_disk_unlocked(
                    account_uid, folder_name, existing, source
                )
            if existing is not None and existing.messages:
                log.debug(
                    "Heavy-folder skip sync rebuild for %s/%s "
                    "(keeping %d indexed headers)",
                    account_uid,
                    folder_name,
                    len(existing.messages),
                )
                return existing, source
            index = self._build_folder_index_unlocked(
                account_uid, folder_name, sync=False
            )
            self._store_folder_index(account_uid, folder_name, index)
            if index.messages:
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    index.messages,
                    index.unread,
                    index.total,
                    grow_only=True,
                )
            return index, "local"

        if sync:
            index = self._build_folder_index_unlocked(
                account_uid, folder_name, sync=True
            )
            kept, kept_source = self._prefer_nonempty_folder_index(
                account_uid, folder_name, index
            )
            if kept_source is not None:
                self._store_folder_index(account_uid, folder_name, kept)
                return kept, kept_source
            self._store_folder_index(account_uid, folder_name, index)
            if _folder_index_is_cacheable(index) and (
                index.messages or folder_index_cache.load(account_uid, folder_name) is None
            ):
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    index.messages,
                    index.unread,
                    index.total,
                    grow_only=heavy,
                )
            return index, "server"

        index = self._folder_indexes.get(key)
        if index is not None:
            if heavy:
                return self._union_heavy_index_with_disk_unlocked(
                    account_uid, folder_name, index, "memory"
                )
            return index, "memory"

        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is not None:
            messages, unread, total = cached
            index = _FolderMessageIndex(
                messages=messages,
                unread=unread,
                total=total,
            )
            self._store_folder_index(account_uid, folder_name, index)
            return index, "disk_cache"

        index = self._build_folder_index_unlocked(
            account_uid, folder_name, sync=False
        )
        self._store_folder_index(account_uid, folder_name, index)
        if index.messages or index.total:
            if _folder_index_is_cacheable(index):
                existing = folder_index_cache.load(account_uid, folder_name)
                # Never replace a larger on-disk index with a partial Camel
                # summary (common for M365 Archive before refresh_info) (#189).
                if existing is not None and len(existing[0]) > len(index.messages):
                    log.debug(
                        "Keeping larger disk cache for %s/%s "
                        "(disk=%d, camel=%d)",
                        account_uid,
                        folder_name,
                        len(existing[0]),
                        len(index.messages),
                    )
                    messages, unread, total = existing
                    index = _FolderMessageIndex(
                        messages=messages,
                        unread=unread,
                        total=total,
                    )
                    self._store_folder_index(account_uid, folder_name, index)
                    return index, "disk_cache"
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    index.messages,
                    index.unread,
                    index.total,
                    grow_only=heavy,
                )
            elif heavy:
                existing = folder_index_cache.load(account_uid, folder_name)
                if existing is not None and len(existing[0]) > len(index.messages):
                    messages, unread, total = existing
                    index = _FolderMessageIndex(
                        messages=messages,
                        unread=unread,
                        total=total,
                    )
                    self._store_folder_index(account_uid, folder_name, index)
                    return index, "disk_cache"
                if _should_save_heavy_folder_index(index.messages, existing):
                    folder_index_cache.save(
                        account_uid,
                        folder_name,
                        index.messages,
                        index.unread,
                        max(index.total, len(index.messages)),
                        grow_only=True,
                    )
        return index, "local"

    def _union_heavy_index_with_disk_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        existing: _FolderMessageIndex,
        source: FolderIndexSource,
    ) -> tuple[_FolderMessageIndex, FolderIndexSource]:
        """Keep disk-only headers in RAM so the list can scroll to them (#365)."""
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is None or not cached[0]:
            return existing, source
        disk_messages, disk_unread, disk_total = cached
        unioned = union_folder_index_messages(
            list(existing.messages), list(disk_messages)
        )
        if len(unioned) <= len(existing.messages):
            return existing, source
        index = _FolderMessageIndex(
            messages=sort_messages_newest_first(unioned),
            unread=max(existing.unread, disk_unread),
            total=max(existing.total, disk_total, len(unioned)),
        )
        self._store_folder_index(account_uid, folder_name, index)
        return index, "disk_cache"

    def _is_missing_folder_error(self, exc: GLib.Error) -> bool:
        return exc.matches(Camel.store_error_quark(), Camel.StoreError.NO_FOLDER)

    def _is_missing_message_error(self, exc: GLib.Error) -> bool:
        return exc.matches(
            Camel.folder_error_quark(), Camel.FolderError.INVALID_UID
        )

    @staticmethod
    def _is_graph_item_not_found_error(exc: BaseException) -> bool:
        """True when Graph says this RestId is gone (stale folder-index UID)."""
        if not isinstance(exc, GLib.Error):
            return False
        text = f"{exc.message or ''} {exc}"
        lowered = text.lower()
        return (
            "ErrorItemNotFound" in text
            or "not found in the store" in lowered
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

    def _folder_index_messages(
        self,
        account_uid: str,
        folder_name: str,
    ) -> list[dict]:
        with self._lock:
            index = self._folder_indexes.get((account_uid, folder_name))
            if index is not None and index.messages:
                return list(index.messages)
        cached = folder_index_cache.load(account_uid, folder_name)
        if cached is None:
            return []
        messages, unread, total = cached
        with self._lock:
            # Seed memory so later recovery sees the same rows.
            existing = self._folder_indexes.get((account_uid, folder_name))
            if existing is None or not existing.messages:
                self._store_folder_index(
                    account_uid,
                    folder_name,
                    _FolderMessageIndex(
                        messages=list(messages),
                        unread=unread,
                        total=total,
                    ),
                )
        return list(messages)

    def _folder_index_has_uid(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> bool:
        return any(
            str(msg.get("uid") or "") == message_uid
            for msg in self._folder_index_messages(account_uid, folder_name)
        )

    def _try_message_cached(
        self,
        folder: Camel.Folder,
        api_uid: str,
    ) -> Any | None:
        file_mime = self._try_message_from_cache_file(folder, api_uid)
        if file_mime is not None:
            return file_mime
        try:
            return folder.get_message_cached(api_uid, None)
        except Exception:
            log.debug("get_message_cached failed for %s", api_uid, exc_info=True)
            return None

    def _first_cached_rfc822_path(
        self,
        folder: Camel.Folder,
        api_uid: str,
    ) -> str | None:
        getter = getattr(folder, "get_filename", None)
        if callable(getter):
            try:
                filename = getter(api_uid)
            except Exception:
                log.debug("get_filename failed for %s", api_uid, exc_info=True)
                filename = None
            if isinstance(filename, str) and filename:
                found = first_nonempty_path(cached_rfc822_candidates(filename))
                if found is not None:
                    return found
        store_uid = None
        parent = getattr(folder, "get_parent_store", None)
        if callable(parent):
            try:
                store = parent()
            except Exception:
                store = None
            uid_get = getattr(store, "get_uid", None) if store is not None else None
            if callable(uid_get):
                try:
                    raw_uid = uid_get()
                except Exception:
                    raw_uid = None
                if isinstance(raw_uid, str) and raw_uid:
                    store_uid = raw_uid
        folder_name = None
        for attr in ("get_full_name", "get_display_name"):
            name_get = getattr(folder, attr, None)
            if not callable(name_get):
                continue
            try:
                raw_name = name_get()
            except Exception:
                continue
            if isinstance(raw_name, str) and raw_name:
                folder_name = raw_name
                break
        if not store_uid:
            return None
        digest = rfc822_digest(api_uid)
        names = [folder_name] if folder_name else []
        if not folder_name or folder_name.lower() == "inbox":
            names.extend(["Inbox", "INBOX"])
        cache_root = os.path.expanduser("~/.cache/evolution")
        for store_root in evolution_store_roots(cache_root, store_uid):
            found = find_nonempty_rfc822(store_root, names, digest)
            if found is not None:
                return found
        return None

    def _construct_mime_from_rfc822_path(self, filename: str) -> Any | None:
        try:
            with open(filename, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if not data:
            return None
        try:
            message = Camel.MimeMessage()
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
            try:
                message.construct_from_input_stream_sync(stream, None)
            finally:
                stream.close()
            return message
        except Exception:
            log.debug(
                "Could not parse cached MIME file %s", filename, exc_info=True
            )
            return None

    def _try_message_from_cache_file(
        self,
        folder: Camel.Folder,
        api_uid: str,
    ) -> Any | None:
        """Load MIME from Camel's on-disk cache file when get_message_cached is empty."""
        path = self._first_cached_rfc822_path(folder, api_uid)
        if path is None:
            return None
        return self._construct_mime_from_rfc822_path(path)

    def _try_fetch_message_uid(
        self,
        folder: Camel.Folder,
        message_uid: str,
    ) -> Any | None:
        """Fetch MIME for ``message_uid`` without recursive recovery (#294)."""
        if camel_uid_is_binary(message_uid):
            return self._try_message_cached(folder, message_uid)
        api_uid = camel_uid_to_api(message_uid)
        cached = self._try_message_cached(folder, api_uid)
        if cached is not None:
            return cached
        try:
            mime = folder.get_message_sync(api_uid, None)
        except GLib.Error as exc:
            if not self._is_missing_message_error(exc):
                raise
            mime = None
            if self._is_graph_item_not_found_error(exc):
                return None
        if mime is not None:
            return mime
        try:
            folder.synchronize_message_sync(api_uid, None)
        except Exception as sync_exc:
            if self._is_graph_item_not_found_error(sync_exc):
                return None
            log.debug(
                "synchronize_message_sync failed for alternate UID %s",
                message_uid,
                exc_info=True,
            )
        try:
            mime = folder.get_message_sync(api_uid, None)
        except GLib.Error as retry_exc:
            if not self._is_missing_message_error(retry_exc):
                raise
            return self._try_message_cached(folder, api_uid)
        if mime is not None:
            return mime
        return self._try_message_cached(folder, api_uid)

    def _folder_index_row(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> dict | None:
        for message in self._folder_index_messages(account_uid, folder_name):
            if str(message.get("uid") or "") == message_uid:
                return message
        return None

    def _remap_folder_index_uid(
        self,
        account_uid: str,
        folder_name: str,
        old_uid: str,
        new_uid: str,
    ) -> None:
        """Replace RestId ``old_uid`` with ``new_uid`` in the folder-index (#294)."""
        if not old_uid or not new_uid or old_uid == new_uid:
            return
        messages = self._folder_index_messages(account_uid, folder_name)
        by_uid: dict[str, dict] = {}
        for message in messages:
            uid = str(message.get("uid") or "")
            if uid:
                by_uid[uid] = dict(message)
        old_row = by_uid.get(old_uid)
        incoming = dict(old_row) if old_row is not None else {"uid": new_uid}
        incoming["uid"] = new_uid
        incoming.pop("moved_provisional", None)
        upsert_folder_index_by_identity(
            by_uid,
            incoming,
            prefer_uids={new_uid},
        )
        by_uid.pop(old_uid, None)
        sorted_messages = self._sorted_folder_messages(by_uid)
        key = (account_uid, folder_name)
        existing = self._folder_indexes.get(key)
        unread = existing.unread if existing is not None else 0
        total = existing.total if existing is not None else len(sorted_messages)
        if existing is not None:
            existing.messages = sorted_messages
            self._merge_correspondents_from_folder(
                account_uid, folder_name, sorted_messages
            )
        else:
            self._store_folder_index(
                account_uid,
                folder_name,
                _FolderMessageIndex(
                    messages=sorted_messages,
                    unread=unread,
                    total=total,
                ),
            )
        folder_index_cache.save(
            account_uid,
            folder_name,
            sorted_messages,
            unread,
            total,
            grow_only=is_heavy_folder_name(folder_name),
        )

    def _drop_stale_folder_index_uid(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> None:
        """Remove a Graph-confirmed-dead RestId from the folder-index (#294)."""
        messages = self._folder_index_messages(account_uid, folder_name)
        row = next(
            (
                message
                for message in messages
                if str(message.get("uid") or "") == message_uid
            ),
            None,
        )
        key = (account_uid, folder_name)
        existing = self._folder_indexes.get(key)
        unread = existing.unread if existing is not None else 0
        total = existing.total if existing is not None else len(messages)
        if (
            row is not None
            and unread >= 0
            and not (row.get("flags") or {}).get("seen", False)
        ):
            unread = max(0, unread - 1)
        if total >= 0:
            total = max(0, total - 1)
        else:
            total = max(0, len(messages) - 1)
        self._remove_messages_from_cache(
            account_uid, folder_name, [message_uid], unread, total
        )

    def _recover_stale_graph_restid(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> Any | None:
        """Fetch a live sibling / dest UID after Graph ErrorItemNotFound (#294)."""
        index_messages = self._folder_index_messages(account_uid, folder_name)
        prefer: set[str] = set()
        try:
            prefer = set(folder_get_uids(folder))
        except Exception:
            log.debug("Could not list Camel UIDs while recovering %s", message_uid)
        siblings = find_folder_index_sibling_uids(
            index_messages,
            message_uid,
            prefer_uids=prefer,
        )
        for sibling in siblings:
            mime = self._try_fetch_message_uid(folder, sibling)
            if mime is None:
                continue
            log.info(
                "Recovered message %s in %r via live RestId %s",
                message_uid,
                folder_name,
                sibling,
            )
            self._remap_folder_index_uid(
                account_uid, folder_name, message_uid, sibling
            )
            self._recovered_read_uid = sibling
            return mime

        row = self._folder_index_row(account_uid, folder_name, message_uid)
        if row is not None and row.get("moved_provisional"):
            backend = self._backend_for_account(account_uid)
            uid_limit = (
                500 if (backend or "").lower() in {"microsoft365", "ews"} else None
            )
            try:
                dest_uids = self._find_moved_uids_in_folder_unlocked(
                    folder,
                    [row],
                    uid_limit=uid_limit,
                    backend=backend,
                )
            except Exception:
                log.debug(
                    "Could not resolve destination UID for provisional %s in %r",
                    message_uid,
                    folder_name,
                    exc_info=True,
                )
                dest_uids = []
            for dest_uid in dest_uids:
                if not dest_uid or dest_uid == message_uid:
                    continue
                mime = self._try_fetch_message_uid(folder, dest_uid)
                if mime is None:
                    continue
                log.info(
                    "Recovered provisional message %s in %r as %s",
                    message_uid,
                    folder_name,
                    dest_uid,
                )
                self._remap_folder_index_uid(
                    account_uid, folder_name, message_uid, dest_uid
                )
                self._recovered_read_uid = dest_uid
                return mime
        return None

    def _recover_online_message_mime(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        api_uid: str,
        *,
        cause: BaseException | None = None,
    ) -> Any:
        """Recover from online INVALID_UID before treating the message as vanished.

        Camel may report INVALID_UID when the folder summary is mid-refresh even
        though the body is cached or the UID is still in Camel (#265). Listed
        messages soft-fail so the row stays; only truly unknown UIDs are vanished.

        Graph ``ErrorItemNotFound`` means that RestId is gone (#294). Remap to a
        same-identity live UID (or resolve ``moved_provisional``), otherwise drop
        the stale index row and raise vanished.
        """
        cached = self._try_message_cached(folder, api_uid)
        if cached is not None:
            log.info(
                "Recovered message %s in %r from local Camel cache after "
                "get_message_sync miss",
                message_uid,
                folder_name,
            )
            return cached

        graph_gone = bool(
            cause is not None and self._is_graph_item_not_found_error(cause)
        )
        camel_known = folder_get_message_info(folder, message_uid) is not None
        index_known = self._folder_index_has_uid(
            account_uid, folder_name, message_uid
        )
        known = camel_known or index_known
        if known and not camel_uid_is_binary(message_uid):
            log.info(
                "Message %s still known in %r but MIME not ready; "
                "synchronizing and retrying",
                message_uid,
                folder_name,
            )
            try:
                folder.synchronize_message_sync(api_uid, None)
            except Exception as sync_exc:
                if self._is_graph_item_not_found_error(sync_exc):
                    log.info(
                        "synchronize_message_sync: Graph item not found for "
                        "%s in %r (stale folder-index UID)",
                        message_uid,
                        folder_name,
                    )
                    graph_gone = True
                    cause = sync_exc
                elif is_sign_in_required_error(sync_exc):
                    cached = self._try_message_cached(folder, api_uid)
                    if cached is not None:
                        self.set_account_connect_health(
                            account_uid, "needs_sign_in"
                        )
                        return cached
                    self._raise_uncached_sign_in(
                        account_uid, folder_name, message_uid, cause=sync_exc
                    )
                else:
                    log_mail_error(
                        log,
                        f"synchronize_message_sync failed for {message_uid} in {folder_name!r}",
                        sync_exc,
                    )
        elif known:
            log.info(
                "Message %s still known in %r but MIME not ready; retrying",
                message_uid,
                folder_name,
            )

        if known:
            try:
                mime = folder.get_message_sync(api_uid, None)
            except GLib.Error as retry_exc:
                if is_sign_in_required_error(retry_exc):
                    cached = self._try_message_cached(folder, api_uid)
                    if cached is not None:
                        self.set_account_connect_health(
                            account_uid, "needs_sign_in"
                        )
                        return cached
                    self._raise_uncached_sign_in(
                        account_uid, folder_name, message_uid, cause=retry_exc
                    )
                if not self._is_missing_message_error(retry_exc):
                    raise
                mime = None
                if self._is_graph_item_not_found_error(retry_exc):
                    graph_gone = True
                cause = retry_exc
            if mime is not None:
                return mime
            cached = self._try_message_cached(folder, api_uid)
            if cached is not None:
                return cached

            if graph_gone:
                recovered = self._recover_stale_graph_restid(
                    folder, account_uid, folder_name, message_uid
                )
                if recovered is not None:
                    return recovered
                log.info(
                    "Dropping Graph-gone RestId %s from %r folder-index",
                    message_uid,
                    folder_name,
                )
                self._drop_stale_folder_index_uid(
                    account_uid, folder_name, message_uid
                )
                err = MessageNotAvailableError(message_uid, folder_name)
                if cause is not None:
                    raise err from cause
                raise err

            # Still listed / known: keep the row and let the user retry (#265).
            err = RuntimeError(
                f"Could not load message {message_uid} in {folder_name!r}"
            )
            if cause is not None:
                raise err from cause
            raise err

        if cause is not None:
            raise MessageNotAvailableError(message_uid, folder_name) from cause
        raise MessageNotAvailableError(message_uid, folder_name)

    def _get_message_sync_with_timeout(
        self,
        folder: Camel.Folder,
        api_uid: str,
    ) -> Any:
        cancellable = Gio.Cancellable()
        timer = threading.Timer(
            _MESSAGE_READ_TIMEOUT_SECONDS, cancellable.cancel
        )
        timer.start()
        try:
            return folder.get_message_sync(api_uid, cancellable)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                cached = self._try_message_cached(folder, api_uid)
                if cached is not None:
                    return cached
                raise TimeoutError(
                    f"Timed out loading message {api_uid}"
                ) from exc
            raise
        finally:
            timer.cancel()

    def _raise_uncached_sign_in(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        self.set_account_connect_health(account_uid, "needs_sign_in")
        error = MessageNotAvailableError(
            message_uid,
            folder_name,
            reason=MessageUnavailableReason.NOT_CACHED_SIGN_IN,
        )
        if cause is not None:
            raise error from cause
        raise error

    def _get_message_mime_sync(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        *,
        allow_network: bool = True,
    ) -> Any:
        offline = not self._network_available
        mime = None
        self._recovered_read_uid = None
        api_uid = camel_uid_to_api(message_uid)
        mime = self._try_message_cached(folder, api_uid)
        if mime is not None:
            return mime
        if not allow_network:
            self._raise_uncached_sign_in(account_uid, folder_name, message_uid)
        try:
            mime = self._get_message_sync_with_timeout(folder, api_uid)
        except TimeoutError:
            self.set_account_connect_health(account_uid, "needs_sign_in")
            mime = self._try_message_cached(folder, api_uid)
            if mime is not None:
                return mime
            raise
        except GLib.Error as exc:
            if self._is_missing_message_error(exc):
                if offline:
                    mime = self._try_message_cached(folder, api_uid)
                    if mime is not None:
                        return mime
                    raise MessageNotAvailableError(
                        message_uid,
                        folder_name,
                        reason=MessageUnavailableReason.NOT_CACHED_OFFLINE,
                    ) from exc
                return self._recover_online_message_mime(
                    folder,
                    account_uid,
                    folder_name,
                    message_uid,
                    api_uid,
                    cause=exc,
                )
            if is_sign_in_required_error(exc):
                self.set_account_connect_health(account_uid, "needs_sign_in")
                mime = self._try_message_cached(folder, api_uid)
                if mime is not None:
                    return mime
                self._raise_uncached_sign_in(
                    account_uid, folder_name, message_uid, cause=exc
                )
            raise
        if mime is None:
            if offline:
                mime = self._try_message_cached(folder, api_uid)
                if mime is not None:
                    return mime
                raise MessageNotAvailableError(
                    message_uid,
                    folder_name,
                    reason=MessageUnavailableReason.NOT_CACHED_OFFLINE,
                )
            return self._recover_online_message_mime(
                folder,
                account_uid,
                folder_name,
                message_uid,
                api_uid,
            )
        return mime

    def _sorted_folder_messages(
        self,
        by_uid: dict[str, dict],
    ) -> list[dict]:
        """Newest-first list from the identity-keyed ``by_uid`` map (#267)."""
        return sort_messages_newest_first(list(by_uid.values()))

    def _try_get_folder_sync_unlocked(
        self,
        store: Camel.Store,
        folder_name: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> Camel.Folder | None:
        try:
            return store.get_folder_sync(folder_name, 0, cancellable)
        except GLib.Error as exc:
            if self._is_missing_folder_error(exc):
                log.debug(
                    "Skipping unavailable folder %r",
                    folder_name,
                )
                return None
            raise

    def _get_named_folder_unlocked(
        self,
        store: Camel.Store,
        folder_name: str,
        cancellable: Gio.Cancellable | None = None,
    ) -> Camel.Folder | None:
        """Open ``folder_name``, trying Inbox/INBOX aliases when the summary is empty."""
        names = _inbox_folder_name_aliases(folder_name)
        chosen: Camel.Folder | None = None
        chosen_count = -1
        for name in names:
            folder = self._try_get_folder_sync_unlocked(store, name, cancellable)
            if folder is None:
                continue
            try:
                count = int(folder.get_message_count())
            except Exception:
                count = 0
            if chosen is None or count > chosen_count:
                chosen = folder
                chosen_count = count
                if count > 0 and name == folder_name:
                    return folder
        return chosen

    def _open_folder_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        cancellable: Gio.Cancellable | None = None,
        allow_online: bool = True,
    ) -> Camel.Folder | None:
        try:
            store = self._get_store_unlocked(
                account_uid, cancellable=cancellable, allow_online=allow_online
            )
        except Exception as exc:
            if not (allow_online and is_sign_in_required_error(exc)):
                raise
            store = self._get_store_unlocked(
                account_uid, cancellable=cancellable, allow_online=False
            )
        try:
            return self._get_named_folder_unlocked(
                store, folder_name, cancellable
            )
        except GLib.Error as exc:
            if not (allow_online and is_sign_in_required_error(exc)):
                raise
            self.set_account_connect_health(account_uid, "needs_sign_in")
            store = self._get_store_unlocked(
                account_uid, cancellable=cancellable, allow_online=False
            )
            return self._get_named_folder_unlocked(
                store, folder_name, cancellable
            )

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
                cancellable = Gio.Cancellable()
                self._register_folder_refresh_cancellable(cancellable)
                try:
                    if cancellable.is_cancelled():
                        raise GLib.Error.new_literal(
                            Gio.io_error_quark(),
                            "Operation was cancelled",
                            Gio.IOErrorEnum.CANCELLED,
                        )
                    folder.refresh_info_sync(cancellable)
                except GLib.Error as refresh_exc:
                    if refresh_exc.matches(
                        Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
                    ):
                        raise
                    if is_sign_in_required_error(refresh_exc):
                        self.set_account_connect_health(
                            account_uid, "needs_sign_in"
                        )
                        log.warning(
                            "refresh_info failed for %s/%s (sign-in required); "
                            "using local Camel summary",
                            account_uid,
                            folder_name,
                        )
                    else:
                        raise
                finally:
                    self._unregister_folder_refresh_cancellable(cancellable)
            unread = folder_get_unread_count(folder)
            total = folder.get_message_count()

            uids = folder_get_uids(folder)
            if not uids:
                return _FolderMessageIndex(messages=[], unread=unread, total=total)

            messages: list[dict] = []
            backend = self._backend_for_account(account_uid)
            for uid in uids:
                info = folder_get_message_info(folder, uid)
                if info is None:
                    continue
                try:
                    messages.append(
                        message_info_to_dict(info, uid=uid, backend=backend)
                    )
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
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                raise
            if self._is_missing_folder_error(exc):
                log.debug(
                    "Skipping unavailable folder %r for account %s",
                    folder_name,
                    account_uid,
                )
                return _FolderMessageIndex(messages=[], unread=0, total=0)
            raise

    def continue_heavy_folder_index(
        self,
        account_uid: str,
        folder_name: str,
        *,
        cursor: dict[str, Any] | None = None,
        allow_refresh: bool = True,
        on_progress: Callable[[HeavyFolderIndexProgress], None] | None = None,
    ) -> HeavyFolderIndexProgress:
        """Advance one preemptible slice of a heavy-folder header index (#208).

        ``allow_refresh=False`` skips ``refresh_info_sync`` and only materializes
        headers already present in the local Camel summary (used when a prior
        refresh timed out, or from callers that must not pin mail I/O).

        ``on_progress`` is invoked from the mail I/O thread when headers grow
        mid-``refresh_info`` (Evolution adds summary rows as Graph delta pages
        arrive; Post mirrors that so the UI is not frozen for minutes).
        """
        return run_on_mail_thread(
            self._continue_heavy_folder_index_unlocked,
            account_uid,
            folder_name,
            cursor=cursor,
            allow_refresh=allow_refresh,
            on_progress=on_progress,
        )

    def _heavy_folder_imap_extra_uids(
        self,
        folder: Any,
        account_uid: str,
        folder_name: str,
        by_uid: dict[str, dict],
        *,
        known_total: int,
        session: dict[str, Any],
        already_tried: bool,
    ) -> list[str]:
        """IMAP UID SEARCH for headers Camel has not summarized yet (#365).

        Microsoft 365 / EWS keep using Graph ``refresh_info``. One attempt per
        folder-open session. Returns only UIDs missing from the grow-only index.
        """
        if already_tried or session.get("did_server_uid_search"):
            return []
        backend = (self._backend_for_account(account_uid) or "").lower()
        if backend not in {"imap", "imapx"}:
            return []
        camel_n = len(folder_get_uids(folder))
        if not _heavy_folder_camel_behind_server(camel_n, known_total):
            cached = folder_status_cache.load(account_uid, folder_name)
            status_total = cached[1] if cached is not None else known_total
            if not _heavy_folder_camel_behind_server(camel_n, status_total):
                if folder_status_cache.index_caught_up(
                    len(by_uid), status_total, folder_name
                ):
                    return []
                if status_total <= 0 or camel_n >= status_total:
                    return []
        try:
            session["did_server_uid_search"] = True
            found = folder_search_all_uids(
                folder,
                "(match-all #t)",
            )
        except Exception:
            session["did_server_uid_search"] = True
            log.debug(
                "Heavy-folder IMAP SEARCH failed for %s/%s",
                account_uid,
                folder_name,
                exc_info=True,
            )
            return []
        extra = [
            str(uid)
            for uid in found
            if uid and str(uid) not in by_uid
        ]
        return extra

    def _continue_heavy_folder_index_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        *,
        cursor: dict[str, Any] | None = None,
        allow_refresh: bool = True,
        on_progress: Callable[[HeavyFolderIndexProgress], None] | None = None,
    ) -> HeavyFolderIndexProgress:
        state = dict(cursor or {})
        key = (account_uid, folder_name)
        backend = self._backend_for_account(account_uid)
        # Correlation id for request → arrive → index → list (#208 debug).
        active_pipeline_id = str(state.get("pipeline_id") or "") or None
        log.debug(
            "Heavy-folder slice start %s/%s allow_refresh=%s cursor_keys=%s "
            "pending_server_refresh=%s refresh_done=%s uid_offset=%s "
            "uid_pending=%s pipeline_id=%s",
            account_uid,
            folder_name,
            allow_refresh,
            sorted(state.keys()),
            state.get("pending_server_refresh"),
            state.get("refresh_done"),
            state.get("uid_offset"),
            len(state["uids"]) if isinstance(state.get("uids"), list) else None,
            active_pipeline_id,
        )

        def _cursor_with_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
            if not active_pipeline_id:
                return payload
            out = dict(payload)
            out.setdefault("pipeline_id", active_pipeline_id)
            return out

        folder = self._open_folder_unlocked(account_uid, folder_name)
        if folder is None:
            log.warning(
                "Heavy-folder slice abort %s/%s: folder not open",
                account_uid,
                folder_name,
            )
            return HeavyFolderIndexProgress(
                messages=[], unread=0, total=0, done=True, cursor={}
            )

        existing_disk = folder_index_cache.load(account_uid, folder_name)
        with self._lock:
            memory = self._folder_indexes.get(key)

        memory_messages = list(memory.messages) if memory is not None else []
        disk_messages = (
            list(existing_disk[0]) if existing_disk is not None else []
        )
        if memory is not None and existing_disk is not None:
            known_messages = union_folder_index_messages(
                memory_messages, disk_messages
            )
            unread = max(memory.unread, existing_disk[1])
            total = max(memory.total, existing_disk[2], len(known_messages))
        elif memory is not None:
            known_messages = memory_messages
            unread = memory.unread
            total = memory.total
        elif existing_disk is not None:
            known_messages, unread, total = existing_disk
            known_messages = list(known_messages)
        else:
            known_messages = []
            unread = 0
            total = 0

        # Prefer sidebar STATUS totals when larger than a partial local summary.
        status = self._cached_folder_stats_unlocked(account_uid, folder_name)
        if status is not None:
            status_unread, status_total = status
            if status_total > total:
                total = status_total
                if status_unread >= 0:
                    unread = status_unread

        known_unread = unread
        known_total = total

        # Collapse already-duplicated disk/memory rows on open (#267).
        # Keep by_identity for O(1) upserts — scanning by_uid is O(n²) on Archive.
        prefer_uids = set(folder_get_uids(folder))
        by_uid: dict[str, dict] = {}
        by_identity: dict[str, str] = {}
        uid_remaps: dict[str, str] = {}
        for msg in known_messages:
            if msg.get("uid"):
                _upsert_message_into_folder_index(
                    by_uid,
                    msg,
                    prefer_uids=prefer_uids,
                    uid_remaps=uid_remaps,
                    by_identity=by_identity,
                )
        if uid_remaps:
            known_messages = self._sorted_folder_messages(by_uid)

        refresh_done = bool(state.get("refresh_done"))
        refresh_attempts = int(state.get("refresh_attempts") or 0)
        refresh_stalls = int(state.get("refresh_stalls") or 0)
        prev_uid_count = int(state.get("uid_count_after_refresh") or 0)
        prev_indexed = int(state.get("indexed_after_refresh") or 0)
        status_seeded = bool(state.get("status_seeded"))
        did_prepare = bool(state.get("did_prepare_content_refresh"))
        did_server_uid_search = bool(state.get("did_server_uid_search"))
        session = self._heavy_index_session(account_uid, folder_name)
        status_seeded = status_seeded or bool(session.get("status_seeded"))
        did_prepare = did_prepare or bool(
            session.get("did_prepare_content_refresh")
        )
        did_server_uid_search = did_server_uid_search or bool(
            session.get("did_server_uid_search")
        )
        uids: list[str] | None = state.get("uids")
        if uids is not None and not isinstance(uids, list):
            uids = None
        uid_offset = int(state.get("uid_offset") or 0)
        pending_server_refresh = bool(state.get("pending_server_refresh"))
        # Offline / explicit skip: local Camel summary only (no Graph refresh).
        local_only = (not allow_refresh) or bool(state.get("refresh_skipped"))

        # Seed STATUS from Microsoft Graph totalItemCount (real ~28k), not Camel
        # summary (~1.3k). Scrub any poisoned high-water that only echoes the
        # local index. Deferred until the refresh phase so local UID indexing
        # can update the list first.
        if (
            allow_refresh
            and not local_only
            and not status_seeded
            and pending_server_refresh
            and is_heavy_folder_name(folder_name)
        ):
            status_seeded = True
            session["status_seeded"] = True
            folder_status_cache.scrub_if_summary_echo(
                account_uid, folder_name, len(known_messages)
            )
            self._seed_heavy_folder_status_from_graph_unlocked(
                account_uid, folder_name, local_indexed=len(known_messages)
            )
            if folder_status_cache.load(account_uid, folder_name) is None:
                try:
                    self._get_account_folder_stats_unlocked(account_uid)
                except Exception:
                    log.debug(
                        "Heavy-folder STATUS seed failed for %s/%s",
                        account_uid,
                        folder_name,
                        exc_info=True,
                    )
            status = self._cached_folder_stats_unlocked(account_uid, folder_name)
            if status is not None:
                status_unread, status_total = status
                if status_total > total:
                    total = status_total
                    if status_unread >= 0:
                        unread = status_unread
                known_total = total
                known_unread = unread

        if not refresh_done:
            if local_only:
                all_uids = folder_get_uids(folder)
                uids = [u for u in all_uids if str(u) not in by_uid]
                uid_offset = 0
                refresh_done = True
            elif get_mail_io_thread().has_interactive_work_pending():
                # Yield without claiming refresh_done — retry next slice (#208).
                log.debug(
                    "Heavy-folder yield for interactive %s/%s indexed=%d",
                    account_uid,
                    folder_name,
                    len(by_uid),
                )
                return HeavyFolderIndexProgress(
                    messages=known_messages,
                    unread=known_unread,
                    total=known_total,
                    done=False,
                    cursor=_cursor_with_pipeline(
                        {
                            "refresh_done": False,
                            "pending_server_refresh": pending_server_refresh,
                            "refresh_attempts": refresh_attempts,
                            "refresh_stalls": refresh_stalls,
                            "uid_count_after_refresh": prev_uid_count,
                            "indexed_after_refresh": prev_indexed,
                            "status_seeded": status_seeded,
                            "did_prepare_content_refresh": did_prepare,
                            "did_server_uid_search": did_server_uid_search,
                            "uids": uids,
                            "uid_offset": uid_offset,
                            "yield_for_interactive": True,
                        }
                    ),
                    uid_remaps=dict(uid_remaps),
                )
            elif not pending_server_refresh:
                # Phase 1: index any local UIDs not already in the grow-only index.
                if uids is None:
                    all_uids = folder_get_uids(folder)
                    uids = [u for u in all_uids if str(u) not in by_uid]
                    uid_offset = 0
            else:
                # Phase 2: pull one or more Graph pages, materialize only NEW UIDs.
                # Re-scanning the whole folder after every +25 page was O(n²) (#208).
                slice_deadline = (
                    time.monotonic() + _HEAVY_FOLDER_REFRESH_SLICE_BUDGET_SECONDS
                )
                pages = 0
                camel_uid_count = prev_uid_count
                while pages < _HEAVY_FOLDER_REFRESH_PAGES_PER_SLICE:
                    if get_mail_io_thread().has_interactive_work_pending():
                        log.debug(
                            "Heavy-folder page loop yield for interactive "
                            "%s/%s pages=%d indexed=%d",
                            account_uid,
                            folder_name,
                            pages,
                            len(by_uid),
                        )
                        break
                    if pages > 0 and time.monotonic() >= slice_deadline:
                        log.debug(
                            "Heavy-folder page loop slice budget exhausted "
                            "%s/%s pages=%d indexed=%d",
                            account_uid,
                            folder_name,
                            pages,
                            len(by_uid),
                        )
                        break
                    cancellable = Gio.Cancellable()
                    self._register_heavy_index_refresh_cancellable(cancellable)
                    started = time.monotonic()
                    refresh_error: BaseException | None = None
                    page_grew = False
                    try:
                        if cancellable.is_cancelled():
                            raise GLib.Error.new_literal(
                                Gio.io_error_quark(),
                                "Operation was cancelled",
                                Gio.IOErrorEnum.CANCELLED,
                            )
                        # Full re-check when forced after an incomplete delta, or
                        # at most once per open while the local index is still
                        # small — prepare_content_refresh resets M365 sync state
                        # and undoes header progress (#208).
                        # Use max(memory, disk): Folder::changed used to wipe
                        # memory while disk still held the progressive index.
                        disk_indexed = (
                            len(existing_disk[0])
                            if existing_disk is not None
                            else 0
                        )
                        indexed_for_prepare = max(len(by_uid), disk_indexed)
                        force_prepare = bool(
                            state.get("force_prepare_incomplete_delta")
                            or session.get("force_prepare_incomplete_delta")
                        )
                        should_prepare = force_prepare or (
                            not did_prepare
                            and pages == 0
                            and indexed_for_prepare
                            < _HEAVY_FOLDER_PREPARE_MIN_INDEXED
                        ) or (
                            not force_prepare
                            and refresh_stalls >= 2
                            and indexed_for_prepare
                            < _HEAVY_FOLDER_PREPARE_MIN_INDEXED
                        )
                        if should_prepare:
                            prepare = getattr(
                                folder, "prepare_content_refresh", None
                            )
                            if callable(prepare):
                                log.info(
                                    "Heavy-folder prepare_content_refresh for "
                                    "%s/%s (attempt=%d stalls=%d page=%d "
                                    "indexed=%d disk=%d force_incomplete=%s)",
                                    account_uid,
                                    folder_name,
                                    refresh_attempts + 1,
                                    refresh_stalls,
                                    pages,
                                    len(by_uid),
                                    disk_indexed,
                                    force_prepare,
                                )
                                prepare()
                                did_prepare = True
                                session["did_prepare_content_refresh"] = True
                                session["force_prepare_incomplete_delta"] = False
                                refresh_stalls = 0
                        elif pages == 0 and not did_prepare:
                            # Mark prepared-skipped so retries do not keep
                            # trying a destructive full re-check.
                            did_prepare = True
                            session["did_prepare_content_refresh"] = True
                            log.info(
                                "Heavy-folder skip prepare_content_refresh for "
                                "%s/%s (indexed=%d disk=%d) — continue delta "
                                "sync",
                                account_uid,
                                folder_name,
                                len(by_uid),
                                disk_indexed,
                            )
                        done_event = threading.Event()
                        finish_error: list[BaseException] = []

                        def _on_refresh_ready(
                            _obj: object | None,
                            result: Gio.AsyncResult,
                            _data: object | None = None,
                        ) -> None:
                            try:
                                folder.refresh_info_finish(result)
                            except BaseException as exc:  # noqa: BLE001
                                finish_error.append(exc)
                            finally:
                                done_event.set()

                        pipeline_id = _next_heavy_pipeline_id()
                        active_pipeline_id = pipeline_id
                        camel_uids_before = len(folder_get_uids(folder))
                        indexed_before = len(by_uid)
                        _log_heavy_pipeline(
                            "request",
                            account_uid,
                            folder_name,
                            pipeline_id=pipeline_id,
                            level=logging.INFO,
                            action="refresh_info",
                            attempt=refresh_attempts + 1,
                            page=pages,
                            indexed=indexed_before,
                            camel_uids=camel_uids_before,
                            did_prepare=did_prepare,
                            stalls=refresh_stalls,
                        )
                        log.debug(
                            "Heavy-folder refresh_info start %s/%s "
                            "id=%s attempt=%d page=%d indexed=%d "
                            "did_prepare=%s stalls=%d (no soft timeout; "
                            "cancel on leave)",
                            account_uid,
                            folder_name,
                            pipeline_id,
                            refresh_attempts + 1,
                            pages,
                            indexed_before,
                            did_prepare,
                            refresh_stalls,
                        )
                        folder.refresh_info(
                            GLib.PRIORITY_DEFAULT,
                            cancellable,
                            _on_refresh_ready,
                            None,
                        )

                        def _persist_and_publish_mid_progress(
                            *, reason: str, arrived: int = 0
                        ) -> None:
                            nonlocal existing_disk, unread, total
                            messages_mid = self._sorted_folder_messages(by_uid)
                            pub_total = max(
                                known_total, total, len(messages_mid)
                            )
                            pub_unread = (
                                known_unread
                                if known_unread >= 0
                                else unread
                            )
                            index_mid = _FolderMessageIndex(
                                messages=messages_mid,
                                unread=pub_unread,
                                total=pub_total,
                            )
                            with self._lock:
                                existing_mem = self._folder_indexes.get(key)
                                if existing_mem is not None:
                                    prefer_mem = set(folder_get_uids(folder))
                                    for msg in existing_mem.messages:
                                        _upsert_message_into_folder_index(
                                            by_uid,
                                            msg,
                                            prefer_uids=prefer_mem,
                                            uid_remaps=uid_remaps,
                                            by_identity=by_identity,
                                        )
                                    messages_mid = (
                                        self._sorted_folder_messages(by_uid)
                                    )
                                    pub_unread = max(
                                        pub_unread, existing_mem.unread
                                    )
                                    pub_total = max(
                                        existing_mem.total,
                                        pub_total,
                                        len(messages_mid),
                                    )
                                    index_mid = _FolderMessageIndex(
                                        messages=messages_mid,
                                        unread=pub_unread,
                                        total=pub_total,
                                    )
                                self._store_folder_index(
                                    account_uid, folder_name, index_mid
                                )
                            if _should_save_heavy_folder_index(
                                messages_mid, existing_disk
                            ):
                                folder_index_cache.save(
                                    account_uid,
                                    folder_name,
                                    messages_mid,
                                    pub_unread,
                                    pub_total,
                                    grow_only=True,
                                )
                                existing_disk = (
                                    messages_mid,
                                    pub_unread,
                                    pub_total,
                                )
                            _log_heavy_pipeline(
                                "index",
                                account_uid,
                                folder_name,
                                pipeline_id=pipeline_id,
                                reason=reason,
                                indexed=len(messages_mid),
                                indexed_delta=max(
                                    0, len(messages_mid) - indexed_before
                                ),
                                arrived_since_request=arrived,
                            )
                            if on_progress is None:
                                return
                            try:
                                on_progress(
                                    HeavyFolderIndexProgress(
                                        messages=messages_mid,
                                        unread=pub_unread,
                                        total=pub_total,
                                        done=False,
                                        cursor={
                                            "refresh_done": False,
                                            "pending_server_refresh": True,
                                            "status_seeded": status_seeded,
                                            "did_prepare_content_refresh": (
                                                did_prepare
                                            ),
                                            "pipeline_id": pipeline_id,
                                        },
                                        uid_remaps=dict(uid_remaps),
                                    )
                                )
                            except Exception:
                                log.debug(
                                    "Heavy-folder mid-refresh progress "
                                    "callback failed",
                                    exc_info=True,
                                )

                        last_camel_uids = camel_uids_before
                        last_rewalk_progress = 0.0
                        last_info_progress_uids = camel_uids_before

                        def _refresh_heartbeat(elapsed: float) -> None:
                            nonlocal last_camel_uids, last_rewalk_progress
                            nonlocal last_info_progress_uids
                            # Evolution adds Camel summary rows as each Graph
                            # delta page arrives inside refresh_info. Poll for
                            # new UIDs here so the message list grows during
                            # the long wait (#208).
                            all_uids_hb = folder_get_uids(folder)
                            camel_now = len(all_uids_hb)
                            arrived_delta = max(0, camel_now - last_camel_uids)
                            behind = _heavy_folder_camel_behind_server(
                                camel_now, known_total
                            )
                            camel_gap = max(0, known_total - camel_now)
                            if arrived_delta:
                                _log_heavy_pipeline(
                                    "arrive",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pipeline_id,
                                    camel_uids=camel_now,
                                    camel_uids_delta=arrived_delta,
                                    elapsed_s=round(elapsed, 1),
                                    source="camel_summary_during_refresh",
                                )
                                step = _HEAVY_FOLDER_INFO_PROGRESS_UID_STEP
                                if (
                                    camel_now // step
                                    > last_info_progress_uids // step
                                ):
                                    last_info_progress_uids = camel_now
                                    log.info(
                                        "Heavy-folder sync progress %s/%s "
                                        "id=%s camel_uids=%d known_total=%d "
                                        "indexed=%d elapsed=%.0fs",
                                        account_uid,
                                        folder_name,
                                        pipeline_id,
                                        camel_now,
                                        known_total,
                                        len(by_uid),
                                        elapsed,
                                    )
                                last_camel_uids = camel_now
                            elif behind and (
                                elapsed - last_rewalk_progress
                                >= _HEAVY_FOLDER_REWALK_PROGRESS_SECONDS
                                or last_rewalk_progress == 0.0
                            ):
                                # Full delta with partial local summary rewalks
                                # known UIDs first — count stays flat (#208).
                                last_rewalk_progress = elapsed
                                _log_heavy_pipeline(
                                    "arrive",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pipeline_id,
                                    camel_uids=camel_now,
                                    camel_uids_delta=0,
                                    known_total=known_total,
                                    camel_gap=camel_gap,
                                    elapsed_s=round(elapsed, 1),
                                    note="rewalking_or_waiting_graph",
                                )
                            pending_hb = [
                                u
                                for u in all_uids_hb
                                if str(u) not in by_uid
                            ]
                            materialized_hb = 0
                            for uid in pending_hb:
                                if (
                                    materialized_hb
                                    >= HEAVY_FOLDER_INDEX_BATCH_SIZE
                                ):
                                    break
                                info = folder_get_message_info(folder, uid)
                                if info is None:
                                    continue
                                try:
                                    _upsert_message_into_folder_index(
                                        by_uid,
                                        message_info_to_dict(info, uid=uid, backend=backend),
                                        prefer_uids=set(all_uids_hb),
                                        uid_remaps=uid_remaps,
                                        by_identity=by_identity,
                                    )
                                    materialized_hb += 1
                                except (OSError, OverflowError, ValueError):
                                    continue
                            rewalk_note = (
                                " rewalk_gap=%d" % camel_gap
                                if behind and not arrived_delta
                                else ""
                            )
                            log.debug(
                                "Heavy-folder refresh_info waiting %s/%s "
                                "id=%s elapsed=%.1fs indexed=%d camel_uids=%d "
                                "new=%d page=%d%s",
                                account_uid,
                                folder_name,
                                pipeline_id,
                                elapsed,
                                len(by_uid),
                                camel_now,
                                materialized_hb,
                                pages,
                                rewalk_note,
                            )
                            if materialized_hb:
                                _persist_and_publish_mid_progress(
                                    reason="heartbeat",
                                    arrived=camel_now - camel_uids_before,
                                )
                            elif behind and (
                                last_rewalk_progress == elapsed
                            ):
                                # Keep UI in Syncing without disk churn (#208).
                                if on_progress is not None:
                                    try:
                                        messages_rw = (
                                            self._sorted_folder_messages(by_uid)
                                        )
                                        on_progress(
                                            HeavyFolderIndexProgress(
                                                messages=messages_rw,
                                                unread=(
                                                    known_unread
                                                    if known_unread >= 0
                                                    else unread
                                                ),
                                                total=max(
                                                    known_total,
                                                    len(messages_rw),
                                                ),
                                                done=False,
                                                cursor={
                                                    "refresh_done": False,
                                                    "pending_server_refresh": True,
                                                    "status_seeded": (
                                                        status_seeded
                                                    ),
                                                    "did_prepare_content_refresh": (
                                                        did_prepare
                                                    ),
                                                    "pipeline_id": pipeline_id,
                                                    "incomplete_delta": True,
                                                    "camel_uids": camel_now,
                                                    "camel_gap": camel_gap,
                                                },
                                            )
                                        )
                                    except Exception:
                                        log.debug(
                                            "Heavy-folder rewalk progress "
                                            "callback failed",
                                            exc_info=True,
                                        )

                        finished = get_mail_io_thread().pump_until(
                            done_event,
                            timeout_seconds=None,
                            run_interactive=True,
                            on_timeout_cancel=None,
                            on_heartbeat=_refresh_heartbeat,
                            heartbeat_seconds=_HEAVY_FOLDER_REFRESH_HEARTBEAT_SECONDS,
                        )
                        if finish_error:
                            raise finish_error[0]
                        if not finished:
                            # Only reached if cancelled while waiting (leave folder).
                            raise GLib.Error.new_literal(
                                Gio.io_error_quark(),
                                "Heavy-folder refresh cancelled",
                                Gio.IOErrorEnum.CANCELLED,
                            )
                        refresh_attempts += 1
                        pages += 1
                        camel_unread = folder_get_unread_count(folder)
                        camel_total = folder.get_message_count()
                        if not (known_total > 0 and camel_total < known_total):
                            unread = camel_unread
                            total = camel_total
                        if is_heavy_folder_name(folder_name) and total >= 0:
                            folder_status_cache.observe(
                                account_uid,
                                folder_name,
                                unread,
                                total,
                                trusted=False,
                            )
                            status_after = folder_status_cache.load(
                                account_uid, folder_name
                            )
                            if status_after is not None:
                                unread, total = (
                                    status_after[0],
                                    max(total, status_after[1]),
                                )
                        all_uids = folder_get_uids(folder)
                        camel_uid_count = len(all_uids)
                        pending = [
                            u for u in all_uids if str(u) not in by_uid
                        ]
                        arrived_total = max(
                            0, camel_uid_count - camel_uids_before
                        )
                        if arrived_total:
                            _log_heavy_pipeline(
                                "arrive",
                                account_uid,
                                folder_name,
                                pipeline_id=pipeline_id,
                                camel_uids=camel_uid_count,
                                camel_uids_delta=arrived_total,
                                source="refresh_info_finished",
                            )
                        log.debug(
                            "Heavy-folder refresh page for %s/%s id=%s: "
                            "%d UIDs (%d new, indexed=%d, camel_total=%d)",
                            account_uid,
                            folder_name,
                            pipeline_id,
                            camel_uid_count,
                            len(pending),
                            len(by_uid),
                            camel_total,
                        )
                        if not pending:
                            refresh_done = True
                            uids = []
                            uid_offset = 0
                            if _heavy_folder_camel_behind_server(
                                camel_uid_count, known_total
                            ):
                                # Graph refresh finished with no new local UIDs
                                # while STATUS still says we are far behind —
                                # incomplete/stale delta. Force prepare a few
                                # times, then keep retrying refresh (#208).
                                prepares_done = int(
                                    session.get("incomplete_prepare_count")
                                    or 0
                                )
                                force_again = (
                                    prepares_done
                                    < _HEAVY_FOLDER_INCOMPLETE_PREPARE_LIMIT
                                )
                                if force_again:
                                    session[
                                        "force_prepare_incomplete_delta"
                                    ] = True
                                    session["did_prepare_content_refresh"] = (
                                        False
                                    )
                                    session["incomplete_prepare_count"] = (
                                        prepares_done + 1
                                    )
                                    did_prepare = False
                                else:
                                    session[
                                        "force_prepare_incomplete_delta"
                                    ] = False
                                _log_heavy_pipeline(
                                    "arrive",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pipeline_id,
                                    level=logging.INFO,
                                    camel_uids=camel_uid_count,
                                    camel_uids_delta=0,
                                    known_total=known_total,
                                    camel_gap=max(
                                        0, known_total - camel_uid_count
                                    ),
                                    pending_new=0,
                                    force_prepare=force_again,
                                    incomplete_prepares=prepares_done
                                    + (1 if force_again else 0),
                                    note="incomplete_delta_keep_alive",
                                )
                                log.info(
                                    "Heavy-folder incomplete delta keep-alive "
                                    "%s/%s camel_uids=%d known_total=%d "
                                    "gap=%d force_prepare=%s prepares=%d",
                                    account_uid,
                                    folder_name,
                                    camel_uid_count,
                                    known_total,
                                    max(0, known_total - camel_uid_count),
                                    force_again,
                                    prepares_done + (1 if force_again else 0),
                                )
                                break
                            refresh_stalls += 1
                            _log_heavy_pipeline(
                                "arrive",
                                account_uid,
                                folder_name,
                                pipeline_id=pipeline_id,
                                camel_uids=camel_uid_count,
                                camel_uids_delta=0,
                                pending_new=0,
                                note="refresh_finished_no_new_uids",
                            )
                            if refresh_stalls >= _HEAVY_FOLDER_REFRESH_STALL_LIMIT:
                                break
                            # Stall: may prepare_content_refresh only while
                            # the local index is still small (#208).
                            continue
                        refresh_stalls = 0
                        page_grew = True
                        # Growth means the delta is moving again (#208).
                        if session.get("force_prepare_incomplete_delta") or (
                            session.get("incomplete_prepare_count")
                        ):
                            session["force_prepare_incomplete_delta"] = False
                            session["incomplete_prepare_count"] = 0
                        # Materialize every new UID from this page now (usually
                        # tens, not thousands) so the next refresh can run soon.
                        materialized_page = 0
                        for uid in pending:
                            if get_mail_io_thread().has_interactive_work_pending():
                                uids = [
                                    u
                                    for u in pending
                                    if str(u) not in by_uid
                                ]
                                uid_offset = 0
                                refresh_done = True
                                break
                            info = folder_get_message_info(folder, uid)
                            if info is None:
                                continue
                            try:
                                _upsert_message_into_folder_index(
                                    by_uid,
                                    message_info_to_dict(info, uid=uid, backend=backend),
                                    prefer_uids={str(u) for u in all_uids},
                                    uid_remaps=uid_remaps,
                                    by_identity=by_identity,
                                )
                                materialized_page += 1
                            except (OSError, OverflowError, ValueError):
                                log.debug(
                                    "Skipping message %r in %r due to invalid "
                                    "metadata",
                                    uid,
                                    folder_name,
                                    exc_info=True,
                                )
                        else:
                            uids = []
                            uid_offset = 0
                            refresh_done = True
                            if materialized_page:
                                _log_heavy_pipeline(
                                    "index",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pipeline_id,
                                    reason="refresh_page",
                                    indexed=len(by_uid),
                                    indexed_delta=materialized_page,
                                    pending_left=0,
                                )
                            # More Graph pages while this slice has budget.
                            continue
                        if materialized_page:
                            _log_heavy_pipeline(
                                "index",
                                account_uid,
                                folder_name,
                                pipeline_id=pipeline_id,
                                reason="refresh_page_partial",
                                indexed=len(by_uid),
                                indexed_delta=materialized_page,
                                pending_left=len(uids) if uids else 0,
                            )
                        break
                    except GLib.Error as exc:
                        refresh_error = exc
                        if exc.matches(
                            Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
                        ):
                            elapsed = time.monotonic() - started
                            refresh_attempts += 1
                            pid = locals().get("pipeline_id") or "unknown"
                            log.warning(
                                "Heavy-folder refresh cancelled after %.1fs "
                                "for %s/%s id=%s (attempt %d); will retry "
                                "while folder stays open (%d known headers)",
                                elapsed,
                                account_uid,
                                folder_name,
                                pid,
                                refresh_attempts,
                                len(by_uid),
                            )
                            unread = known_unread
                            total = known_total
                            all_uids = folder_get_uids(folder)
                            camel_uid_count = len(all_uids)
                            pending = [
                                u for u in all_uids if str(u) not in by_uid
                            ]
                            camel_before = locals().get("camel_uids_before")
                            if isinstance(camel_before, int) and (
                                camel_uid_count > camel_before
                            ):
                                _log_heavy_pipeline(
                                    "arrive",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pid,
                                    camel_uids=camel_uid_count,
                                    camel_uids_delta=(
                                        camel_uid_count - camel_before
                                    ),
                                    source="cancelled_but_camel_grew",
                                )
                            # Materialize a batch of any UIDs Camel already has
                            # so soft-timeout yields move the indexed count (#208).
                            materialized = 0
                            camel_prefer = {str(u) for u in all_uids}
                            for uid in pending:
                                if materialized >= HEAVY_FOLDER_INDEX_BATCH_SIZE:
                                    break
                                if get_mail_io_thread().has_interactive_work_pending():
                                    break
                                info = folder_get_message_info(folder, uid)
                                if info is None:
                                    continue
                                try:
                                    _upsert_message_into_folder_index(
                                        by_uid,
                                        message_info_to_dict(info, uid=uid, backend=backend),
                                        prefer_uids=camel_prefer,
                                        uid_remaps=uid_remaps,
                                        by_identity=by_identity,
                                    )
                                    materialized += 1
                                except (OSError, OverflowError, ValueError):
                                    log.debug(
                                        "Skipping message %r in %r due to "
                                        "invalid metadata",
                                        uid,
                                        folder_name,
                                        exc_info=True,
                                    )
                            uids = [
                                u for u in all_uids if str(u) not in by_uid
                            ]
                            uid_offset = 0
                            refresh_done = bool(uids)
                            pending_server_refresh = True
                            if materialized:
                                _log_heavy_pipeline(
                                    "index",
                                    account_uid,
                                    folder_name,
                                    pipeline_id=pid,
                                    reason="cancel_yield",
                                    indexed=len(by_uid),
                                    indexed_delta=materialized,
                                )
                                log.debug(
                                    "Heavy-folder soft-yield materialized %d "
                                    "headers for %s/%s (indexed=%d)",
                                    materialized,
                                    account_uid,
                                    folder_name,
                                    len(by_uid),
                                )
                        elif self._is_missing_folder_error(exc):
                            return HeavyFolderIndexProgress(
                                messages=[],
                                unread=0,
                                total=0,
                                done=True,
                                cursor={},
                            )
                        else:
                            raise
                        break
                    finally:
                        self._unregister_heavy_index_refresh_cancellable(
                            cancellable
                        )
                        if refresh_error is not None:
                            log.debug(
                                "Heavy-folder refresh ended for %s/%s: %s",
                                account_uid,
                                folder_name,
                                refresh_error,
                            )
                    if not page_grew and refresh_error is not None:
                        break

                prev_uid_count = camel_uid_count
                prev_indexed = len(by_uid)

        if uids is None:
            uids = []
        if (
            not uids
            and allow_refresh
            and not local_only
        ):
            extra_uids = self._heavy_folder_imap_extra_uids(
                folder,
                account_uid,
                folder_name,
                by_uid,
                known_total=max(known_total, total, len(by_uid)),
                session=session,
                already_tried=did_server_uid_search,
            )
            did_server_uid_search = did_server_uid_search or bool(
                session.get("did_server_uid_search")
            )
            if extra_uids:
                uids = extra_uids
                uid_offset = 0
                log.info(
                    "Heavy-folder IMAP SEARCH queued %d extra UIDs for "
                    "%s/%s (indexed=%d)",
                    len(extra_uids),
                    account_uid,
                    folder_name,
                    len(by_uid),
                )
        if not uids:
            # No pending UIDs to materialize — persist and decide whether to
            # refresh again.
            messages = self._sorted_folder_messages(by_uid)
            if known_total > total:
                total = known_total
                unread = known_unread
            # Safe prune when Camel summary matches trusted STATUS (#267).
            if is_heavy_folder_name(folder_name):
                camel_prefer = {str(u) for u in folder_get_uids(folder)}
                status_for_prune = status[1] if status is not None else known_total
                cached_for_prune = folder_status_cache.load(
                    account_uid, folder_name
                )
                if cached_for_prune is not None:
                    status_for_prune = max(
                        status_for_prune, cached_for_prune[1]
                    )
                if (
                    folder_status_cache.index_caught_up(
                        len(camel_prefer), status_for_prune, folder_name
                    )
                    and not _heavy_folder_camel_behind_server(
                        len(camel_prefer), known_total
                    )
                ):
                    if prune_stale_folder_index_uids(by_uid, camel_prefer, by_identity=by_identity):
                        messages = self._sorted_folder_messages(by_uid)
            index = _FolderMessageIndex(
                messages=messages, unread=unread, total=total
            )
            with self._lock:
                existing_mem = self._folder_indexes.get(key)
                if existing_mem is not None:
                    prefer_mem = set(folder_get_uids(folder))
                    for msg in existing_mem.messages:
                        _upsert_message_into_folder_index(
                            by_uid,
                            msg,
                            prefer_uids=prefer_mem,
                            uid_remaps=uid_remaps,
                            by_identity=by_identity,
                        )
                    messages = self._sorted_folder_messages(by_uid)
                    unread = max(unread, existing_mem.unread)
                    total = max(existing_mem.total, total, len(messages))
                    index = _FolderMessageIndex(
                        messages=messages, unread=unread, total=total
                    )
                self._store_folder_index(account_uid, folder_name, index)
            if is_heavy_folder_name(folder_name):
                if _should_save_heavy_folder_index(messages, existing_disk):
                    save_total = max(total, len(messages))
                    save_unread = (
                        unread if total >= save_total else known_unread
                    )
                    folder_index_cache.save(
                        account_uid,
                        folder_name,
                        messages,
                        save_unread,
                        save_total,
                        grow_only=True,
                    )
            elif existing_disk is None or len(messages) >= len(existing_disk[0]):
                if _folder_index_is_cacheable(index):
                    save_total = max(total, len(messages))
                    save_unread = unread if total >= save_total else known_unread
                    folder_index_cache.save(
                        account_uid,
                        folder_name,
                        messages,
                        save_unread,
                        save_total,
                    )
            status_total = status[1] if status is not None else total
            if is_heavy_folder_name(folder_name):
                cached_status = folder_status_cache.load(account_uid, folder_name)
                if cached_status is not None:
                    status_total = max(status_total, cached_status[1])
                    total = max(total, cached_status[1])
            behind_status = not folder_status_cache.index_caught_up(
                len(messages), status_total, folder_name
            )
            # Spam/Trash/Junk are often <1000 messages. Without a locked STATUS
            # total, behind_status stays true forever and the indexer never
            # finishes (status flickers "from server" ↔ "so far"). After several
            # no-growth refreshes, lock STATUS from the local index (#208).
            if (
                behind_status
                and is_trash_or_junk_folder_name(folder_name)
                and not folder_status_cache.status_total_is_trusted(
                    folder_name, status_total
                )
                and refresh_stalls >= 2
            ):
                folder_status_cache.observe(
                    account_uid,
                    folder_name,
                    unread if unread >= 0 else 0,
                    len(messages),
                    trusted=True,
                )
                status_total = len(messages)
                total = max(total, status_total)
                behind_status = False
                log.info(
                    "Heavy-folder Trash/Junk STATUS locked from local index "
                    "%s/%s indexed=%d stalls=%d",
                    account_uid,
                    folder_name,
                    len(messages),
                    refresh_stalls,
                )
            # Keep chasing while behind STATUS or incomplete-delta keep-alive.
            # Do not continue solely because stalls < limit — that left caught-up
            # folders refreshing and flickering status forever (#208 Spam).
            force_incomplete = bool(
                session.get("force_prepare_incomplete_delta")
            )
            if allow_refresh and not local_only and (
                behind_status or force_incomplete
            ):
                log.debug(
                    "Heavy-folder slice continue refresh %s/%s indexed=%d "
                    "status_total=%d behind=%s stalls=%d attempts=%d "
                    "force_incomplete=%s",
                    account_uid,
                    folder_name,
                    len(messages),
                    status_total,
                    behind_status,
                    refresh_stalls,
                    refresh_attempts,
                    force_incomplete,
                )
                return HeavyFolderIndexProgress(
                    messages=messages,
                    unread=unread,
                    total=max(total, status_total, len(messages)),
                    done=False,
                    cursor=_cursor_with_pipeline(
                        {
                            "refresh_done": False,
                            "pending_server_refresh": True,
                            "refresh_attempts": refresh_attempts,
                            "refresh_stalls": refresh_stalls,
                            "uid_count_after_refresh": prev_uid_count,
                            "indexed_after_refresh": len(messages),
                            "status_seeded": status_seeded,
                            "did_prepare_content_refresh": did_prepare,
                            "did_server_uid_search": did_server_uid_search,
                            "force_prepare_incomplete_delta": force_incomplete,
                            "incomplete_delta": force_incomplete or behind_status,
                        }
                    ),
                    uid_remaps=dict(uid_remaps),
                )
            log.info(
                "Heavy-folder slice done %s/%s indexed=%d status_total=%d "
                "stalls=%d attempts=%d",
                account_uid,
                folder_name,
                len(messages),
                status_total,
                refresh_stalls,
                refresh_attempts,
            )
            return HeavyFolderIndexProgress(
                messages=messages,
                unread=unread,
                total=total,
                done=True,
                cursor=_cursor_with_pipeline({}),
                uid_remaps=dict(uid_remaps),
            )

        end = min(uid_offset + HEAVY_FOLDER_INDEX_BATCH_SIZE, len(uids))
        processed = 0
        camel_prefer = {str(u) for u in folder_get_uids(folder)}
        for uid in uids[uid_offset:end]:
            if get_mail_io_thread().has_interactive_work_pending():
                break
            if str(uid) in by_uid:
                processed += 1
                continue
            info = folder_get_message_info(folder, uid)
            if info is None:
                processed += 1
                continue
            try:
                _upsert_message_into_folder_index(
                    by_uid,
                    message_info_to_dict(info, uid=uid, backend=backend),
                    prefer_uids=camel_prefer,
                    uid_remaps=uid_remaps,
                    by_identity=by_identity,
                )
            except (OSError, OverflowError, ValueError):
                log.debug(
                    "Skipping message %r in %r due to invalid metadata",
                    uid,
                    folder_name,
                    exc_info=True,
                )
            processed += 1
        uid_offset += processed

        # Drop stale RestIds only when Camel is caught up vs trusted STATUS (#267).
        if is_heavy_folder_name(folder_name):
            status_for_prune = status[1] if status is not None else known_total
            cached_for_prune = folder_status_cache.load(account_uid, folder_name)
            if cached_for_prune is not None:
                status_for_prune = max(status_for_prune, cached_for_prune[1])
            if (
                folder_status_cache.index_caught_up(
                    len(camel_prefer), status_for_prune, folder_name
                )
                and not _heavy_folder_camel_behind_server(
                    len(camel_prefer), known_total
                )
            ):
                prune_stale_folder_index_uids(by_uid, camel_prefer, by_identity=by_identity)

        messages = self._sorted_folder_messages(by_uid)
        # Prefer larger known totals when Camel summary is still partial.
        if known_total > total:
            total = known_total
            unread = known_unread
        index = _FolderMessageIndex(messages=messages, unread=unread, total=total)
        with self._lock:
            existing_mem = self._folder_indexes.get(key)
            if existing_mem is not None:
                prefer_mem = set(folder_get_uids(folder))
                for msg in existing_mem.messages:
                    _upsert_message_into_folder_index(
                        by_uid,
                        msg,
                        prefer_uids=prefer_mem,
                        uid_remaps=uid_remaps,
                        by_identity=by_identity,
                    )
                messages = self._sorted_folder_messages(by_uid)
                unread = max(unread, existing_mem.unread)
                total = max(existing_mem.total, total, len(messages))
                index = _FolderMessageIndex(
                    messages=messages, unread=unread, total=total
                )
            self._store_folder_index(account_uid, folder_name, index)

        if is_heavy_folder_name(folder_name):
            if _should_save_heavy_folder_index(messages, existing_disk):
                save_total = max(total, len(messages))
                save_unread = (
                    unread if total >= save_total else known_unread
                )
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    messages,
                    save_unread,
                    save_total,
                    grow_only=True,
                )
        elif existing_disk is None or len(messages) >= len(existing_disk[0]):
            if _folder_index_is_cacheable(index):
                # Persist message rows; keep max(total, len) so partial Camel
                # counts cannot shrink a prior STATUS-sized total on disk.
                save_total = max(total, len(messages))
                save_unread = unread if total >= save_total else known_unread
                folder_index_cache.save(
                    account_uid,
                    folder_name,
                    messages,
                    save_unread,
                    save_total,
                )

        batch_complete = uid_offset >= len(uids)
        next_cursor: dict[str, Any] = {}
        done = False
        status_total = status[1] if status is not None else total
        if is_heavy_folder_name(folder_name):
            cached_status = folder_status_cache.load(account_uid, folder_name)
            if cached_status is not None:
                status_total = max(status_total, cached_status[1])
                total = max(total, cached_status[1])
                unread = cached_status[0] if cached_status[1] >= total else unread

        def _cursor_base(**extra: Any) -> dict[str, Any]:
            base = {
                "refresh_attempts": refresh_attempts,
                "refresh_stalls": refresh_stalls,
                "uid_count_after_refresh": prev_uid_count,
                "indexed_after_refresh": prev_indexed,
                "status_seeded": status_seeded,
                "did_prepare_content_refresh": did_prepare,
                "did_server_uid_search": did_server_uid_search,
            }
            base.update(extra)
            return _cursor_with_pipeline(base)

        if not batch_complete:
            next_cursor = _cursor_base(
                refresh_done=refresh_done,
                pending_server_refresh=False,
                uids=uids,
                uid_offset=uid_offset,
            )
        elif allow_refresh and not local_only:
            extra_uids = self._heavy_folder_imap_extra_uids(
                folder,
                account_uid,
                folder_name,
                by_uid,
                known_total=max(known_total, total, len(messages)),
                session=session,
                already_tried=did_server_uid_search,
            )
            did_server_uid_search = did_server_uid_search or bool(
                session.get("did_server_uid_search")
            )
            if extra_uids:
                log.info(
                    "Heavy-folder IMAP SEARCH queued %d extra UIDs for "
                    "%s/%s (indexed=%d)",
                    len(extra_uids),
                    account_uid,
                    folder_name,
                    len(messages),
                )
                next_cursor = _cursor_base(
                    refresh_done=False,
                    pending_server_refresh=False,
                    uids=extra_uids,
                    uid_offset=0,
                )
            else:
                # Local/new UIDs indexed — fetch more server headers (#208).
                next_cursor = _cursor_base(
                    refresh_done=False,
                    pending_server_refresh=True,
                    indexed_after_refresh=len(messages),
                )
        else:
            done = True

        log.debug(
            "Heavy-folder slice materialize %s/%s indexed=%d processed=%d "
            "uid_offset=%d/%d batch_complete=%s done=%s "
            "pending_server_refresh=%s",
            account_uid,
            folder_name,
            len(messages),
            processed,
            uid_offset,
            len(uids),
            batch_complete,
            done,
            bool(next_cursor.get("pending_server_refresh")),
        )
        if processed:
            _log_heavy_pipeline(
                "index",
                account_uid,
                folder_name,
                pipeline_id=str(active_pipeline_id or "batch"),
                reason="uid_batch",
                indexed=len(messages),
                indexed_delta=processed,
                uid_offset=uid_offset,
                uid_pending=len(uids),
                batch_complete=batch_complete,
            )
        return HeavyFolderIndexProgress(
            messages=messages,
            unread=unread,
            total=total,
            done=done,
            cursor=next_cursor,
            uid_remaps=dict(uid_remaps),
        )

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

    def read_compose_attachments(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> list[ComposeAttachment]:
        """Load all attachment payloads for compose (e.g. forward) in one MIME fetch."""
        return run_on_mail_thread(
            self._read_compose_attachments_unlocked,
            account_uid,
            folder_name,
            message_uid,
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

    def _goa_credentials_ready_for_read_unlocked(self, account_uid: str) -> bool:
        """Return False when GOA cannot mint a token (skip Graph, try cache)."""
        source = self.registry.ref_source(account_uid)
        if source is None or not source.has_extension("GNOME Online Accounts"):
            return True
        if ensure_goa_credentials(self.registry, source, None):
            return True
        self.set_account_connect_health(account_uid, "needs_sign_in")
        return False

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

        if self.get_account_connect_health(account_uid) == "needs_sign_in":
            allow_network = False
        else:
            allow_network = self._goa_credentials_ready_for_read_unlocked(
                account_uid
            )
        try:
            folder = self._open_folder_unlocked(
                account_uid, folder_name, allow_online=allow_network
            )
        except Exception as exc:
            if not allow_network or is_sign_in_required_error(exc):
                self._raise_uncached_sign_in(
                    account_uid, folder_name, message_uid, cause=exc
                )
            raise
        if folder is None:
            if not allow_network:
                self._raise_uncached_sign_in(account_uid, folder_name, message_uid)
            raise ValueError(f"Folder not found: {folder_name}")
        if self.get_account_connect_health(account_uid) == "needs_sign_in":
            allow_network = False

        info = folder_get_message_info(folder,message_uid)
        was_unread = info is not None and not (
            info.get_flags() & Camel.MessageFlags.SEEN
        )

        mime = self._get_message_mime_sync(
            folder,
            account_uid,
            folder_name,
            message_uid,
            allow_network=allow_network,
        )
        actual_uid = self._recovered_read_uid or message_uid
        if actual_uid != message_uid:
            remapped_info = folder_get_message_info(folder, actual_uid)
            if remapped_info is not None:
                info = remapped_info
                was_unread = not (
                    info.get_flags() & Camel.MessageFlags.SEEN
                )

        result = (
            message_info_to_dict(
                info, backend=self._backend_for_account(account_uid)
            )
            if info
            else {"uid": actual_uid}
        )
        result["uid"] = actual_uid
        if actual_uid != message_uid:
            result["_previous_uid"] = message_uid
        enrich_message_dict_from_mime(result, mime)
        bodies = extract_message_bodies(mime)
        if not (bodies.get("plain") or bodies.get("html")):
            file_mime = self._try_message_from_cache_file(
                folder, camel_uid_to_api(actual_uid)
            )
            if file_mime is not None:
                mime = file_mime
                enrich_message_dict_from_mime(result, mime)
                bodies = extract_message_bodies(mime)
        if not (bodies.get("plain") or bodies.get("html")):
            path = self._first_cached_rfc822_path(
                folder, camel_uid_to_api(actual_uid)
            )
            if path:
                try:
                    with open(path, "rb") as handle:
                        raw = handle.read()
                except OSError:
                    raw = b""
                if raw:
                    from .helpers import extract_message_bodies_from_bytes

                    bodies = extract_message_bodies_from_bytes(raw)
        if not (bodies.get("plain") or bodies.get("html")) and not allow_network:
            self._raise_uncached_sign_in(
                account_uid, folder_name, actual_uid
            )
        result["body_plain"] = bodies["plain"]
        result["body_html"] = bodies["html"]
        result["attachments"] = extract_attachments(mime)
        result["inline_images"] = extract_inline_images(mime)
        invite = self._calendar_invite_for_mime(
            mime,
            attachments=result["attachments"],
            bodies=bodies,
            subject=result.get("subject"),
        )
        if invite is not None:
            result["calendar_invite"] = invite
        if not result.get("message_id") and hasattr(mime, "get_message_id"):
            result["message_id"] = mime.get_message_id()
        if hasattr(mime, "get_header"):
            references = mime.get_header("References")
            if references:
                result["references"] = normalize_references_header(references)

        if was_unread and mark_seen and (
            bodies.get("plain") or bodies.get("html")
        ):
            try:
                unread, total = self._mark_message_seen_unlocked(
                    folder, account_uid, folder_name, actual_uid
                )
                result.setdefault("flags", {})["seen"] = True
                result["folder_unread"] = unread
                result["folder_total"] = total
            except Exception as exc:
                if is_sign_in_required_error(exc):
                    self.set_account_connect_health(account_uid, "needs_sign_in")
                log_mail_error(log, "Mark-seen sync failed", exc)
                # Local \\Seen may already be set; do not discard the read result.
                result.setdefault("flags", {})["seen"] = True
                result["folder_unread"] = folder_get_unread_count(folder)
                result["folder_total"] = folder.get_message_count()

        return result

    @staticmethod
    def _calendar_invite_for_mime(
        mime: Any,
        *,
        attachments: list[dict],
        bodies: dict[str, str | None],
        subject: str | None,
    ) -> dict | None:
        from .calendar_invite import looks_like_calendar_attachment, merge_invite_details
        from .helpers import get_attachment_data

        ics_text = None
        attachment_index = None
        preferred_index = None
        for meta in attachments:
            mime_type = meta.get("mime_type")
            filename = meta.get("filename")
            if not looks_like_calendar_attachment(
                mime_type if isinstance(mime_type, str) else None,
                filename if isinstance(filename, str) else None,
            ):
                continue
            index = meta.get("index")
            if not isinstance(index, int):
                continue
            try:
                _name, data = get_attachment_data(mime, index)
            except Exception:
                continue
            if not data:
                continue
            text = data.decode("utf-8", errors="replace")
            method = str(meta.get("calendar_method") or "")
            if preferred_index is None:
                ics_text = text
                attachment_index = index
                preferred_index = index
            if method.upper() == "REQUEST" or "METHOD:REQUEST" in text.upper():
                ics_text = text
                attachment_index = index
                break
        return merge_invite_details(
            subject=subject if isinstance(subject, str) else None,
            ics_text=ics_text,
            body_plain=bodies.get("plain"),
            body_html=bodies.get("html"),
            attachment_index=attachment_index,
        )

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

        mime = self._get_message_mime_sync(
            folder, account_uid, folder_name, message_uid
        )

        return get_attachment_data(mime, attachment_index)

    def _read_compose_attachments_unlocked(
        self,
        account_uid: str,
        folder_name: str,
        message_uid: str,
    ) -> list[ComposeAttachment]:
        store = self._get_store_unlocked(account_uid)
        folder = store.get_folder_sync(folder_name, 0, None)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_name}")

        mime = self._get_message_mime_sync(
            folder, account_uid, folder_name, message_uid
        )
        return read_compose_attachments_from_message(mime)

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
            return folder_get_unread_count(folder), folder.get_message_count()

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
        unread = folder_get_unread_count(folder)
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

        backend = self._backend_for_account(account_uid)
        currently_flagged = _message_info_is_flagged(info, backend=backend)
        new_flagged = not currently_flagged
        changed = self._apply_message_flagged_unlocked(
            folder,
            account_uid,
            folder_name,
            message_uid,
            new_flagged,
            backend=backend,
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
        unread = folder_get_unread_count(folder)
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
        backend = self._backend_for_account(account_uid)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder_get_message_info(folder,message_uid)
            if info is None:
                continue
            currently_flagged = _message_info_is_flagged(info, backend=backend)
            if currently_flagged == flagged:
                updates.append({"uid": message_uid, "flags": {"flagged": flagged}})
                continue
            if self._apply_message_flagged_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                flagged,
                backend=backend,
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
        unread = folder_get_unread_count(folder)
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
        backend = self._backend_for_account(account_uid)
        updates: list[dict[str, Any]] = []
        changed_uids: list[str] = []
        for message_uid in message_uids:
            info = folder_get_message_info(folder,message_uid)
            if info is None:
                continue
            currently_flagged = _message_info_is_flagged(info, backend=backend)
            new_flagged = not currently_flagged
            if self._apply_message_flagged_unlocked(
                folder,
                account_uid,
                folder_name,
                message_uid,
                new_flagged,
                backend=backend,
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
        source_unread = folder_get_unread_count(source_folder)
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
        with self._lock:
            folders = self._folder_tree_cache.get(account_uid)
        if folders is None:
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
            raise ValueError("No matching messages to move")

        if not self._network_available and not self._flushing_operation_queue:
            return self._queue_transfer_operation_unlocked(
                account_uid,
                source_folder_name,
                transfer_uids,
                op_type=op_type,
                destination_folder=dest_name,
            )

        account = self._accounts_by_uid.get(account_uid)
        backend = (account.backend or "").lower() if account is not None else ""
        # Fail-fast while a previous Graph move is still pinning the mail thread
        # or the UI already marked the account not-responding (#189).
        if backend in {"microsoft365", "ews"}:
            transfer_state = self.get_account_transfer_state(account_uid)
            if transfer_state != "idle":
                raise RuntimeError(
                    "A previous move is still in progress or the server is not "
                    "responding; try again in a moment"
                )

        source_messages = self._message_dicts_for_uids_unlocked(
            source_folder,
            transfer_uids,
            backend=backend or None,
        )

        destination_uids: list[str] = []
        moved_uids = list(transfer_uids)
        cancellable = Gio.Cancellable()

        def _on_transfer_timeout() -> None:
            cancellable.cancel()
            # Escalate badge even if Camel has not returned yet (#189).
            if self.get_account_transfer_state(account_uid) == "busy":
                self.set_account_transfer_state(account_uid, "not_responding")

        timer = threading.Timer(_TRANSFER_TIMEOUT_SECONDS, _on_transfer_timeout)
        self.set_account_transfer_state(account_uid, "busy")
        timer.start()
        transfer_start = time.monotonic()
        # O365 transfer_messages_to_sync refreshes the destination unless it is
        # frozen — that refresh often hangs after Graph already moved the mail.
        # Evolution freezes folders around bulk transfers for the same reason.
        source_folder.freeze()
        destination_folder.freeze()
        try:
            try:
                log.debug(
                    "transfer_messages_to_sync start account=%s %s → %s uids=%d",
                    account_uid,
                    source_folder_name,
                    dest_name,
                    len(transfer_uids),
                )
                for offset in range(0, len(transfer_uids), _TRANSFER_MESSAGE_BATCH_SIZE):
                    batch = transfer_uids[offset : offset + _TRANSFER_MESSAGE_BATCH_SIZE]
                    ok, transferred = source_folder.transfer_messages_to_sync(
                        batch, destination_folder, True, cancellable
                    )
                    if not ok:
                        raise RuntimeError("Could not move messages")
                    destination_uids.extend(camel_uid_list(transferred))
                log.debug(
                    "transfer_messages_to_sync done account=%s in %.2fs",
                    account_uid,
                    time.monotonic() - transfer_start,
                )
            except Exception as exc:
                timed_out = cancellable.is_cancelled() or (
                    isinstance(exc, GLib.Error)
                    and exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
                )
                if timed_out:
                    gone = self._uids_missing_from_folder_unlocked(
                        source_folder, transfer_uids
                    )
                    if gone:
                        log.warning(
                            "Transfer timed out after %.1fs for %s (%s → %s) but "
                            "%d/%d message(s) left the source; treating as moved",
                            time.monotonic() - transfer_start,
                            account_uid,
                            source_folder_name,
                            dest_name,
                            len(gone),
                            len(transfer_uids),
                        )
                        moved_uids = gone
                        # Keep destination_uids from batches that already returned
                        # them — clearing here disabled Undo after Archive All on
                        # M365 when a later batch hangs (#189/#261).
                    else:
                        log.warning(
                            "Transfer timed out after %.1fs for %s (%s → %s); "
                            "messages still in source",
                            time.monotonic() - transfer_start,
                            account_uid,
                            source_folder_name,
                            dest_name,
                        )
                        raise TimeoutError(
                            f"Move timed out after {_TRANSFER_TIMEOUT_SECONDS}s"
                        ) from exc
                elif (
                    is_queueable_network_error(exc)
                    and not self._flushing_operation_queue
                ):
                    return self._queue_transfer_operation_unlocked(
                        account_uid,
                        source_folder_name,
                        transfer_uids,
                        op_type=op_type,
                        destination_folder=dest_name,
                    )
                else:
                    raise
            finally:
                timer.cancel()
                try:
                    destination_folder.thaw()
                except Exception:
                    log.debug("destination thaw failed", exc_info=True)
                try:
                    source_folder.thaw()
                except Exception:
                    log.debug("source thaw failed", exc_info=True)

            # Commit + refresh can hang indefinitely on M365 after a successful
            # Graph move. Bound them so the UI can complete; cache is updated below.
            self._finalize_folder_transfer_unlocked(
                account_uid,
                source_folder,
                destination_folder,
                source_folder_name=source_folder_name,
                dest_name=dest_name,
            )

            # Evolution-style: drop moved UIDs from Camel's local summary so the
            # folder matches Post's cache without a hung Graph refresh (#189).
            self._prune_folder_summary_uids_unlocked(source_folder, moved_uids)

            if not destination_uids:
                # Full Archive fingerprint scans are costly / can hang on Graph.
                # Still try a capped newest-UID match so Archive cache can show
                # the moved message with a real destination UID (#189).
                try:
                    destination_uids = self._find_moved_uids_in_folder_unlocked(
                        destination_folder,
                        source_messages,
                        uid_limit=(
                            500 if backend in {"microsoft365", "ews"} else None
                        ),
                        backend=backend or None,
                    )
                except Exception:
                    log.debug(
                        "Could not resolve destination UIDs after move",
                        exc_info=True,
                    )
                    destination_uids = []

            source_unread = folder_get_unread_count(source_folder)
            source_total = source_folder.get_message_count()
            self._remove_messages_from_cache(
                account_uid, source_folder_name, moved_uids, source_unread, source_total
            )
            # Never wipe the destination disk index after a move. Invalidating
            # Archive then rebuilding from an incomplete Camel summary was saving
            # ~50 messages as the full folder and OOMing on the next startup
            # background reindex (#189).
            dest_unread, dest_total = self._merge_moved_messages_into_dest_cache_unlocked(
                account_uid,
                dest_name,
                source_messages=source_messages,
                destination_uids=destination_uids,
                moved_count=len(moved_uids),
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
        finally:
            self.set_account_transfer_state(account_uid, "idle")

    @staticmethod
    def _prune_folder_summary_uids_unlocked(
        folder: Camel.Folder,
        message_uids: list[str],
    ) -> None:
        """Remove UIDs from Camel's local FolderSummary (Evolution-style, #189).

        Best-effort: never fails the move if summary mutation is unavailable.
        """
        if not message_uids:
            return
        try:
            summary = folder.get_folder_summary()
        except Exception:
            log.debug("Could not get folder summary for prune", exc_info=True)
            return
        if summary is None:
            return

        api_uids = [camel_uid_to_api(uid) for uid in message_uids]
        try:
            removed = False
            remove_uids = getattr(summary, "remove_uids", None)
            if callable(remove_uids):
                try:
                    removed = bool(remove_uids(api_uids))
                except Exception:
                    log.debug("summary.remove_uids failed; falling back", exc_info=True)
            if not removed:
                for uid in api_uids:
                    try:
                        if summary.remove_uid(uid):
                            removed = True
                    except Exception:
                        log.debug(
                            "summary.remove_uid failed for %r",
                            uid,
                            exc_info=True,
                        )
            if removed:
                summary.touch()
                summary.save()
        except Exception:
            log.debug("Folder summary prune failed", exc_info=True)
            return

        try:
            changes = Camel.FolderChangeInfo.new()
            for uid in api_uids:
                changes.remove_uid(uid)
            folder.changed(changes)
        except Exception:
            log.debug("folder.changed after summary prune failed", exc_info=True)

    @staticmethod
    def _uids_missing_from_folder_unlocked(
        folder: Camel.Folder, message_uids: list[str]
    ) -> list[str]:
        """Return UIDs that are no longer present in ``folder``."""
        missing: list[str] = []
        for uid in message_uids:
            try:
                if folder_get_message_info(folder, uid) is None:
                    missing.append(uid)
            except Exception:
                # Treat lookup failures as still-present; caller may time out.
                log.debug(
                    "Could not probe UID %r after transfer timeout",
                    uid,
                    exc_info=True,
                )
        return missing

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
            if not self._flushing_operation_queue and op_type is not None and (
                is_queueable_network_error(exc)
                or is_sign_in_required_error(exc)
            ):
                if is_sign_in_required_error(exc):
                    self.set_account_connect_health(account_uid, "needs_sign_in")
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
        cancellable: Gio.Cancellable | None = None,
    ) -> None:
        """Push a folder transfer to the mail store (required for IMAP)."""
        if not source_folder.synchronize_sync(True, cancellable):
            raise RuntimeError("Could not synchronize source folder after move")
        destination_folder.synchronize_sync(False, cancellable)

    def _finalize_folder_transfer_unlocked(
        self,
        account_uid: str,
        source_folder: Camel.Folder,
        destination_folder: Camel.Folder,
        *,
        source_folder_name: str,
        dest_name: str | None,
    ) -> None:
        """Best-effort post-move sync/refresh with a hard timeout.

        Graph/M365 often completes ``transfer_messages_to_sync`` then hangs in
        synchronize/refresh. The move already succeeded; do not block the UI.
        """
        account = self._accounts_by_uid.get(account_uid)
        backend = (account.backend or "").lower() if account is not None else ""
        # Graph/EWS already applied the move server-side. Local summary is
        # updated via cache; synchronize/refresh_info often hang (#189).
        if backend in {"microsoft365", "ews"}:
            log.debug(
                "Skipping post-transfer sync/refresh for %s backend=%s (%s → %s)",
                account_uid,
                backend,
                source_folder_name,
                dest_name,
            )
            return

        cancellable = Gio.Cancellable()
        timer = threading.Timer(
            _TRANSFER_POST_TIMEOUT_SECONDS, cancellable.cancel
        )
        timer.start()
        try:
            self._commit_folder_transfer_unlocked(
                source_folder, destination_folder, cancellable
            )
            if cancellable.is_cancelled():
                log.warning(
                    "Post-transfer sync timed out for %s (%s → %s)",
                    account_uid,
                    source_folder_name,
                    dest_name,
                )
                return
            source_folder.refresh_info_sync(cancellable)
            if cancellable.is_cancelled():
                log.warning(
                    "Post-transfer source refresh timed out for %s/%s",
                    account_uid,
                    source_folder_name,
                )
                return
            destination_folder.refresh_info_sync(cancellable)
            if cancellable.is_cancelled():
                log.warning(
                    "Post-transfer destination refresh timed out for %s/%s",
                    account_uid,
                    dest_name,
                )
        except GLib.Error as exc:
            if (
                cancellable.is_cancelled()
                or exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)
                or is_network_unavailable_error(exc)
            ):
                log.warning(
                    "Post-transfer sync/refresh incomplete for %s (%s → %s): %s",
                    account_uid,
                    source_folder_name,
                    dest_name,
                    exc.message,
                )
                return
            raise
        except RuntimeError as exc:
            if cancellable.is_cancelled():
                log.warning(
                    "Post-transfer sync incomplete for %s (%s → %s): %s",
                    account_uid,
                    source_folder_name,
                    dest_name,
                    exc,
                )
                return
            raise
        finally:
            timer.cancel()

    def _message_dicts_for_uids_unlocked(
        self,
        folder: Camel.Folder,
        message_uids: list[str],
        *,
        backend: str | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for message_uid in message_uids:
            info = folder_get_message_info(folder,message_uid)
            if info is not None:
                messages.append(message_info_to_dict(info, backend=backend))
        return messages

    def _find_moved_uids_in_folder_unlocked(
        self,
        folder: Camel.Folder,
        source_messages: list[dict[str, Any]],
        *,
        uid_limit: int | None = None,
        backend: str | None = None,
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

        if uid_limit is None:
            for uid in uids:
                info = folder_get_message_info(folder, uid)
                if info is None:
                    continue
                message = message_info_to_dict(info, uid=uid, backend=backend)
                fingerprint = (
                    message.get("subject") or "",
                    message.get("from") or "",
                    message.get("sort_date") or 0,
                )
                if fingerprint in fingerprints:
                    found.append(str(uid))
                    if len(found) >= len(fingerprints):
                        break
            return found

        # Graph/EWS: Camel UID order is not newest-first. Score by date, then
        # match only among the newest *uid_limit* headers (#189/#261).
        dated: list[tuple[float, str, dict[str, Any]]] = []
        for uid in uids:
            info = folder_get_message_info(folder, uid)
            if info is None:
                continue
            message = message_info_to_dict(info, uid=uid, backend=backend)
            dated.append(
                (float(message.get("sort_date") or 0), str(uid), message)
            )
        dated.sort(key=lambda item: item[0], reverse=True)
        for _sort_date, uid, message in dated[:uid_limit]:
            fingerprint = (
                message.get("subject") or "",
                message.get("from") or "",
                message.get("sort_date") or 0,
            )
            if fingerprint in fingerprints:
                found.append(uid)
                if len(found) >= len(fingerprints):
                    break
        return found

    def _backend_for_account(self, account_uid: str) -> str | None:
        account = self._accounts_by_uid.get(account_uid)
        if account is None:
            return None
        return account.backend

    def _apply_message_flagged_unlocked(
        self,
        folder: Camel.Folder,
        account_uid: str,
        folder_name: str,
        message_uid: str,
        flagged: bool,
        *,
        backend: str | None = None,
    ) -> bool:
        if backend is None:
            backend = self._backend_for_account(account_uid)
        return _apply_message_flagged(
            folder,
            message_uid,
            flagged,
            backend=backend,
            on_flagged_changed=lambda value: self._update_cached_message_flags(
                account_uid, folder_name, message_uid, flagged=value
            ),
        )

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
        # Copy-on-write: never mutate flags dicts that may still be referenced by
        # MessageListItem rows. In-place updates make the context menu read the
        # new value while the list flag icon stays stale until rebind (#289).
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is None:
            return
        for position, message in enumerate(index.messages):
            if message.get("uid") == message_uid:
                merged = dict(message.get("flags") or {})
                if seen is not None:
                    merged["seen"] = seen
                if flagged is not None:
                    merged["flagged"] = flagged
                updated = dict(message)
                updated["flags"] = merged
                index.messages[position] = updated
                break

    def _update_cached_folder_counts(
        self, account_uid: str, folder_name: str, unread: int, total: int
    ) -> None:
        index = self._folder_indexes.get((account_uid, folder_name))
        if index is not None:
            index.unread = unread
            index.total = total

    def _merge_moved_messages_into_dest_cache_unlocked(
        self,
        account_uid: str,
        dest_name: str | None,
        *,
        source_messages: list[dict],
        destination_uids: list[str],
        moved_count: int,
    ) -> tuple[int, int]:
        """Update destination folder cache after a move without wiping it.

        Returns ``(unread, total)`` for sidebar badges. Always prepend moved
        message rows so Archive shows them immediately — even when Graph gives
        no destination UIDs (provisional source UIDs) (#189).
        """
        if not dest_name:
            return -1, -1

        key = (account_uid, dest_name)
        index = self._folder_indexes.get(key)
        if index is None:
            cached = folder_index_cache.load(account_uid, dest_name)
            if cached is not None:
                messages, unread, total = cached
                index = _FolderMessageIndex(
                    messages=list(messages),
                    unread=unread,
                    total=total,
                )
                self._store_folder_index(account_uid, dest_name, index)

        if index is None:
            # No prior cache — seed from the moved messages only (do not pull a
            # partial Camel Archive summary that would poison disk cache).
            if not source_messages and not destination_uids:
                return -1, -1
            index = _FolderMessageIndex(messages=[], unread=0, total=0)
            self._store_folder_index(account_uid, dest_name, index)

        by_order = list(source_messages)
        new_messages: list[dict] = []
        if destination_uids:
            for offset, dest_uid in enumerate(destination_uids):
                base = dict(by_order[offset]) if offset < len(by_order) else {}
                base["uid"] = dest_uid
                base.pop("moved_provisional", None)
                new_messages.append(base)
        else:
            # Graph often returns no dest UIDs. Keep headers visible in Archive
            # using source UIDs until a later resolve/refresh.
            for message in by_order:
                base = dict(message)
                if not base.get("uid"):
                    continue
                base["moved_provisional"] = True
                new_messages.append(base)

        if not new_messages:
            if moved_count > 0 and index.total >= 0:
                index.total += max(0, moved_count)
                folder_index_cache.save(
                    account_uid,
                    dest_name,
                    index.messages,
                    index.unread,
                    index.total,
                )
            return index.unread, index.total

        def _fingerprint(message: dict) -> tuple[str, str, int | float]:
            return (
                message.get("subject") or "",
                message.get("from") or "",
                message.get("sort_date") or 0,
            )

        existing_uids = {
            message.get("uid") for message in index.messages if message.get("uid")
        }
        prepend: list[dict] = []
        added_unread = 0
        for message in new_messages:
            uid = message.get("uid")
            fingerprint = _fingerprint(message)
            if uid and uid in existing_uids:
                continue

            replaced = False
            if fingerprint != ("", "", 0):
                for offset, existing in enumerate(index.messages):
                    if _fingerprint(existing) != fingerprint:
                        continue
                    if existing.get("moved_provisional") and not message.get(
                        "moved_provisional"
                    ):
                        index.messages[offset] = message
                        existing_uids.add(uid)
                        replaced = True
                    else:
                        replaced = True
                    break
            if replaced:
                continue

            prepend.append(message)
            if uid:
                existing_uids.add(uid)
            if not (message.get("flags") or {}).get("seen", False):
                added_unread += 1

        if prepend:
            index.messages = prepend + index.messages
            if index.unread >= 0:
                index.unread += added_unread
            if index.total >= 0:
                index.total += len(prepend)
            else:
                index.total = len(index.messages)
            self._merge_correspondents_from_folder(
                account_uid, dest_name, prepend
            )

        folder_index_cache.save(
            account_uid,
            dest_name,
            index.messages,
            index.unread,
            index.total,
        )
        return index.unread, index.total

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
            # Still drop from disk so a later cache seed cannot resurrect UIDs.
            cached = folder_index_cache.load(account_uid, folder_name)
            if cached is None:
                return
            messages, _cached_unread, _cached_total = cached
            messages = [
                message
                for message in messages
                if message.get("uid") not in uid_set
            ]
            folder_index_cache.save(
                account_uid, folder_name, messages, unread, total
            )
            return
        index.messages = [
            message
            for message in index.messages
            if message.get("uid") not in uid_set
        ]
        index.unread = unread
        index.total = total
        folder_index_cache.save(
            account_uid,
            folder_name,
            index.messages,
            index.unread,
            index.total,
        )

    def _invalidate_folder_index(
        self, account_uid: str, folder_name: str | None
    ) -> None:
        if not folder_name:
            return
        # Archive/Trash/Junk indexes are grown progressively across Graph pages.
        # Folder::changed during refresh/cancel must not delete that work — a
        # follow-up Camel summary rebuild collapses thousands of headers and
        # then prepare_content_refresh forces a full delta reset (#208).
        if is_heavy_folder_name(folder_name):
            existing = self._folder_indexes.get((account_uid, folder_name))
            disk = folder_index_cache.load(account_uid, folder_name)
            kept = max(
                len(existing.messages) if existing is not None else 0,
                len(disk[0]) if disk is not None else 0,
            )
            log.info(
                "Heavy-folder skip index invalidate for %s/%s "
                "(keeping %d indexed headers)",
                account_uid,
                folder_name,
                kept,
            )
            return
        self._folder_indexes.pop((account_uid, folder_name), None)
        folder_index_cache.invalidate(account_uid, folder_name)

    @staticmethod
    def guess_inbox(folders: list[dict]) -> str | None:
        return guess_inbox_name(folders)
