# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main application window — 3-pane mail layout."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from post.compose_window import ComposeWindow, SavedDraftNotification
from post.credentials import prompt_password_sync
from post.folder_dialogs import confirm_action
from post.gtk_schedule import schedule_on_gtk_main
from post.header_bar import add_end_window_controls
from post.icon_utils import apply_window_icon
from post.mail import MailService
from post.mail.eds import (
    MailAccount,
    MessageNotAvailableError,
    MessageUnavailableReason,
    OfflineSyncProgress,
    _log_heavy_pipeline,
)
from post.mail.io_thread import get_mail_io_thread
from post.mail.mailto import MailtoCompose, parse_mailto_uri
from post.mail.sync_watcher import MailSyncWatcher
from post.message_list_view import VirtualMessageList
from post.open_uri import open_uri_externally
from post.mail.folders import (
    POST_OUTBOX_FOLDER,
    format_account_refresh_done,
    format_account_refresh_error,
    format_account_refresh_start,
    format_folder_refresh_done,
    format_folder_refresh_error,
    format_folder_refresh_start,
    format_startup_loading_accounts,
    is_post_outbox_folder,
)
from post.mail.folder_index_cache import (
    has_cache as folder_index_has_cache,
    load as load_folder_index_cache,
)
from post.mail.message_list_state import (
    MESSAGE_LIST_UI_BATCH_SIZE,
    MESSAGE_LIST_UI_BIND_CAP,
    MESSAGE_LIST_UI_BIND_MORE,
    is_heavy_folder_name,
    message_flag_patches,
    message_list_fingerprint,
    message_lists_equivalent_for_ui,
    prepended_message_count,
)
from post.mail.message_flags import FOLLOW_UP_FLAG_BACKENDS
from post.mail.search import (
    MessageSearchQuery,
    SearchFilterProgress,
    annotate_search_match,
    filter_messages_by_query,
    filter_search_matches_for_folder,
    format_search_filter_progress,
    format_search_result_meta,
    format_search_target_label,
    group_list_keys_by_location,
    make_search_row_key,
    parse_search_query,
    parse_search_row_key,
    query_requires_body_scan,
    search_filter_progress_fraction,
)
from post.mail.search_debug import search_trace, search_trace_timer
from post.mail.operation_queue import offline_queue_status_text
from post.mail.send_delay import OutboundSendDelayScheduler
from post.mail.network_errors import (
    format_message_read_error,
    is_network_unavailable_error,
    is_sign_in_required_error,
    log_mail_error,
)
from post.mail.offline_status import (
    OFFLINE_CACHED_LIST_STATUS,
    OFFLINE_MAIL_MESSAGE,
    OFFLINE_SEARCHING_LOCAL_CACHE,
    offline_cache_status_text,
)
from post.mail.send_queue import (
    QueuedOutboundMessage,
    format_status_send_now_tooltip,
    format_stop_sending_error_toast,
    format_stop_sending_toast,
    has_pending_send_delay,
    list_pending_delayed_outbound_messages,
    list_queued_messages,
    list_queued_outbound_messages,
    load_queued_attachments,
    load_queued_outbound_message,
    read_queued_message,
    remaining_send_delay_seconds,
    remove_queued_outbound_message,
    soonest_pending_send_after,
    try_load_queued_outbound_message,
)
from post.settings_window import SettingsWindow
from post.mail.helpers import (
    flag_menu_items,
    flag_menu_label,
    format_from_search_query,
    insert_messages_newest_first,
    message_matches_bulk_archive_scope,
    perform_one_click_unsubscribe,
    read_menu_items,
    read_menu_label,
    reader_toggle_button_state,
    should_offer_send_again,
    sort_messages_newest_first,
    uniform_bool_state,
    write_temp_attachment,
)
from post.message_list_activate import (
    MessageListActivateAction,
    message_list_activate_action,
)
from post.reader.pane import MessageReaderPane
from post.reader_window import ReaderWindow
from post.preferences import (
    MessageAppearance,
    MIN_MESSAGE_LIST_WIDTH,
    MIN_SIDEBAR_WIDTH,
    OFFLINE_BODY_SYNC_ALL,
    OFFLINE_BODY_SYNC_LAST_MONTH,
    OFFLINE_BODY_SYNC_LAST_YEAR,
    OFFLINE_BODY_SYNC_OFF,
    OfflineBodySyncMode,
    SEARCH_SCOPE_ACCOUNT,
    SEARCH_SCOPE_ALL,
    SEARCH_SCOPE_FOLDER,
    SearchScope,
    format_send_delay_status,
    get_load_remote_content,
    get_message_appearance,
    get_search_scope,
    get_sidebar_state,
    get_window_state,
    set_account_offline_body_sync,
    set_active_message_uid,
    set_offline_body_sync_prompt_declined,
    set_search_scope,
    should_show_offline_body_sync_prompt,
    set_window_state,
)
from post.mail.offline_settings import account_is_user_offline
from post.sidebar import MailSidebar
from post.toast import show_error_toast, show_toast
from post.wrap_label import WrappingLabel

log = logging.getLogger(__name__)

MESSAGE_LIST_SYNC_STATUS = "Syncing with Server"

_SIDEBAR_TOP_INSET = 12
_SEARCH_PROGRESS_UI_INTERVAL_US = 100_000
_CACHED_HEADER_SEARCH_CHUNK_SIZE = 200

_MESSAGE_LIST_CSS = f"""
listview.message-list row {{
  padding-top: 0;
  padding-bottom: 0;
}}
listview.message-list label {{
  min-width: 0;
}}
list.navigation-sidebar {{
  margin-top: 0;
  padding-top: 0;
}}
list.navigation-sidebar label,
expander.sidebar-section label {{
  min-width: 0;
}}
expander.sidebar-section > title {{
  min-height: 0;
  padding-top: 4px;
  padding-bottom: 4px;
}}
expander.sidebar-section {{
  margin-top: 0;
  padding-top: 0;
}}
/* Single 1px divider (theme wide-handle / borders look like a double line). */
paned.horizontal > separator {{
  min-width: 1px;
  margin: 0;
  padding: 0;
  border: none;
  background-color: alpha(@window_fg_color, 0.12);
  box-shadow: none;
}}
separator.header-divider {{
  min-height: 1px;
  background-color: alpha(@window_fg_color, 0.12);
}}
box.calendar-invite {{
  margin-top: 2px;
  margin-bottom: 2px;
  padding: 10px 12px;
  border-radius: 8px;
  border-top: 1px solid alpha(@window_fg_color, 0.14);
  border-bottom: 1px solid alpha(@window_fg_color, 0.14);
  background-color: alpha(@window_fg_color, 0.05);
}}
label.calendar-invite-link {{
  color: @accent_color;
}}
.message-unread-dot {{
  min-width: 10px;
  min-height: 10px;
  border-radius: 999px;
  background-color: @accent_bg_color;
}}
.message-flagged-icon {{
  color: @error_color;
}}
progressbar.status-search-progress {{
  min-height: 3px;
  margin-top: 2px;
  margin-bottom: 0;
  padding-top: 0;
  padding-bottom: 0;
}}
progressbar.status-search-progress trough,
progressbar.status-search-progress progress {{
  min-height: 3px;
  border-radius: 1px;
}}
progressbar.status-search-progress trough {{
  background-color: alpha(@window_fg_color, 0.1);
}}
progressbar.status-search-progress-indeterminate progress {{
  background-color: @accent_bg_color;
}}
button.message-flagged {{
  color: @error_color;
}}
button.message-read-action {{
  opacity: 1;
}}
list.navigation-sidebar row.drop-highlight {{
  background-color: alpha(@accent_bg_color, 0.18);
}}
"""


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._install_message_list_style()
        self.set_title("Post")
        apply_window_icon(self)
        window_state = get_window_state()
        self._last_normal_width = int(window_state["width"])
        self._last_normal_height = int(window_state["height"])
        self.set_default_size(self._last_normal_width, self._last_normal_height)
        if window_state.get("maximized"):
            self.maximize()

        self.connect("close-request", self._on_close_request)
        self.connect("notify::width", self._on_window_size_changed)
        self.connect("notify::height", self._on_window_size_changed)
        self.connect("notify::is-active", self._on_is_active_changed)
        self._close_after_outbound_send = False
        self._close_after_folder_transfer = False
        self._delayed_send_close_dialog: Adw.AlertDialog | None = None
        self._is_closing = False
        self._pending_goa_reauth: set[str] = set()

        self._mail = MailService.connect()
        self._stop_sending_in_flight = False
        self._send_delay_scheduler = OutboundSendDelayScheduler(
            self._mail,
            on_outbox_changed=self._on_outbox_changed,
        )
        self._mail.set_password_prompt(self._prompt_account_password)
        self._mail.set_account_health_changed_callback(self._on_account_health_changed)
        self._sync_watcher = MailSyncWatcher(
            self._mail,
            on_folder_changed=self._on_sync_folder_changed,
            on_folder_tree_changed=self._on_sync_folder_tree_changed,
        )
        self._mail.set_sync_setup_cancel_callback(self._sync_watcher.cancel_setup)
        self._folder_count_poll_timer_id: int | None = None
        self._folder_count_poll_deferred_id: int | None = None
        self._open_folder_poll_timer_id: int | None = None
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._current_message_uid: str | None = None
        self._current_message: dict | None = None
        self._messages_load_generation = 0
        self._message_read_generation = 0
        self._pending_message_read_uid: str | None = None
        self._inflight_message_read_id: int | None = None
        self._message_total = -1
        self._current_folder_messages: list[dict] | None = None
        self._message_list_bound_count = 0
        self._message_list_source = ""
        self._message_sync_in_progress = False
        self._message_list_populating = False
        self._pending_sync_folder_refresh: tuple[str, str] | None = None
        self._context_attachment_index: int | None = None
        self._context_attachment_mime: str | None = None
        self._context_attachment_name: str | None = None
        self._context_message_uids: list[str] = []
        self._pending_move_undo: dict | None = None
        self._undo_toast: Adw.Toast | None = None
        self._bulk_archive_progress_toast: Adw.Toast | None = None
        self._settings_dialog: SettingsWindow | None = None
        self._compose_windows: list[ComposeWindow] = []
        self._reader_windows: list[ReaderWindow] = []
        self._load_remote_content = get_load_remote_content()
        self._message_appearance = get_message_appearance()
        self._restore_message_folder: tuple[str, str] | None = None
        self._pending_restore_message_uid: str | None = None
        self._suppress_sync_list_reload: tuple[str, str] | None = None
        self._local_draft_sync_suppress_until: dict[tuple[str, str], float] = {}
        self._user_message_click_pending = False
        self._search_query: MessageSearchQuery | None = None
        self._search_scope = get_search_scope()
        self._search_scope_items: list[SearchScope] = []
        self._search_scope_dropdown_updating = False
        self._folder_before_multi_folder_search: tuple[str, str] | None = None
        self._search_entry_updating = False
        self._messages_load_expects_search = False
        self._pre_search_snapshot: tuple[list[dict], int, int, str] | None = None
        self._pre_search_folder: tuple[str, str] | None = None
        self._search_progress_last_ui_time = 0
        self._search_results_streamed = False
        self._status_progress_pulse_id: int | None = None
        self._offline_download_status = ""
        self._status_hint = ""
        self._send_delay_status = ""
        self._send_delay_tick_id: int | None = None
        self._offline_held_for_load_generation: int | None = None
        self._heavy_index_in_progress: tuple[str, str] | None = None
        self._heavy_bind_catchup_load_id: int | None = None
        self._heavy_pipeline_id: str | None = None
        self._network_available = Gio.NetworkMonitor.get_default().get_network_available()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_vexpand(True)

        header = Adw.HeaderBar()
        add_end_window_controls(header)

        self._header_search_entry = Gtk.SearchEntry()
        self._header_search_entry.set_placeholder_text(
            "Search…  from: to: subject: cc: body: is:(!)read is:(!)flagged has:(!)attachment"
        )
        self._header_search_entry.set_size_request(546, -1)
        self._header_search_entry.set_hexpand(True)
        self._header_search_entry.set_alignment(0.0)
        self._header_search_entry.set_sensitive(False)
        self._header_search_entry.set_search_delay(300)
        self._header_search_entry.connect("search-changed", self._on_search_changed)
        self._header_search_entry.connect("activate", self._on_search_activate)
        self._header_search_entry.connect("stop-search", self._on_search_stopped)

        self._search_scope_dropdown = Gtk.DropDown()
        self._search_scope_dropdown.set_sensitive(False)
        self._search_scope_dropdown.set_hexpand(False)
        self._search_scope_dropdown.set_valign(Gtk.Align.CENTER)
        self._search_scope_dropdown.connect(
            "notify::selected", self._on_search_scope_changed
        )

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_row.add_css_class("linked")
        search_row.set_halign(Gtk.Align.FILL)
        search_row.set_hexpand(True)
        search_row.set_valign(Gtk.Align.CENTER)
        search_row.append(self._search_scope_dropdown)
        search_row.append(self._header_search_entry)

        search_title = Gtk.Box()
        search_title.set_halign(Gtk.Align.FILL)
        search_title.set_hexpand(True)
        search_title.set_valign(Gtk.Align.CENTER)
        search_title.set_margin_start(48)
        search_title.set_margin_end(48)
        search_title.append(search_row)
        header.set_title_widget(search_title)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.set_margin_start(8)
        settings_btn.connect("clicked", self._on_settings_clicked)

        header_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_actions.set_margin_end(10)
        header_actions.set_valign(Gtk.Align.CENTER)

        compose_btn = Gtk.Button(icon_name="mail-message-new-symbolic")
        compose_btn.set_tooltip_text("New Message (Ctrl+N)")
        compose_btn.connect("clicked", self._on_compose_new_clicked)
        header_actions.append(compose_btn)

        self._header_read_btn = Gtk.Button(icon_name="mail-mark-read-symbolic")
        self._header_read_btn.set_tooltip_text("Mark as Read")
        self._header_read_btn.add_css_class("message-read-action")
        self._header_read_btn.set_sensitive(False)
        self._header_read_btn.connect("clicked", self._on_header_read_toggle_clicked)
        header_actions.append(self._header_read_btn)

        self._header_flag_btn = Gtk.Button(icon_name="mail-flag-symbolic")
        self._header_flag_btn.set_tooltip_text("Flag")
        self._header_flag_btn.add_css_class("message-flagged")
        self._header_flag_btn.set_sensitive(False)
        self._header_flag_btn.connect("clicked", self._on_header_flag_toggle_clicked)
        header_actions.append(self._header_flag_btn)

        self._header_archive_btn = Gtk.Button(icon_name="mail-archive-symbolic")
        self._header_archive_btn.set_tooltip_text("Archive")
        self._header_archive_btn.set_sensitive(False)
        self._header_archive_btn.connect("clicked", self._on_header_archive_clicked)
        header_actions.append(self._header_archive_btn)

        self._header_trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
        self._header_trash_btn.set_tooltip_text("Move to Trash")
        self._header_trash_btn.set_sensitive(False)
        self._header_trash_btn.connect("clicked", self._on_header_trash_clicked)
        header_actions.append(self._header_trash_btn)
        header_actions.append(settings_btn)

        header.pack_end(header_actions)

        header_divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        header_divider.add_css_class("header-divider")
        outer.append(header_divider)

        self._sidebar_width = int(window_state["sidebar_width"])
        self._message_list_width = int(window_state["message_list_width"])
        self._pane_widths_ready = False
        self._pane_map_restored = False

        self._outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned.set_vexpand(True)
        self._outer_paned.set_hexpand(True)
        self._outer_paned.set_overflow(Gtk.Overflow.HIDDEN)
        # Narrow handle: single separator line (wide handle looks like a double line).
        self._outer_paned.set_wide_handle(False)
        self._outer_paned.set_resize_start_child(False)
        self._outer_paned.set_resize_end_child(True)
        # shrink=True allocates the start child at a negative x when dragged below
        # its minimum, which clips content on the left (#229).
        self._outer_paned.set_shrink_start_child(False)
        self._outer_paned.set_shrink_end_child(True)
        outer.append(self._outer_paned)

        self._inner_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._inner_paned.set_vexpand(True)
        self._inner_paned.set_hexpand(True)
        self._inner_paned.set_overflow(Gtk.Overflow.HIDDEN)
        self._inner_paned.set_wide_handle(False)
        # Keep message-list width stable on window resize; only the drag handle
        # (and restore) should change it (#229).
        self._inner_paned.set_resize_start_child(False)
        self._inner_paned.set_resize_end_child(True)
        self._inner_paned.set_shrink_start_child(False)
        self._inner_paned.set_shrink_end_child(True)

        self._sidebar = MailSidebar(
            self._mail,
            on_folder_selected=self._on_folder_selected,
            set_status=self._set_status,
            on_refresh_account=self._on_sidebar_refresh_account,
            on_refresh_folder=self._on_sidebar_refresh_folder,
            on_send_outbox=self._on_sidebar_send_outbox,
            on_accounts_loaded=self._on_accounts_loaded,
            on_initial_folder_load_complete=self._on_initial_folder_load_complete,
            on_folder_tree_ready=self._on_folder_tree_ready,
            on_folder_tree_changed=self._on_sidebar_folder_tree_changed,
            on_folder_contents_changed=self._on_sidebar_folder_contents_changed,
            on_move_started=self._on_sidebar_move_started,
            on_bulk_archive_finished=self._on_sidebar_bulk_archive_finished,
            on_bulk_archive_error=self._on_sidebar_bulk_archive_error,
            on_move_undo_available=self._on_sidebar_move_undo_available,
            on_account_online_changed=self._on_account_online_changed,
            on_goa_reauth_requested=self._on_goa_reauth_requested,
            on_messages_dropped=self._on_messages_dropped,
        )
        sidebar_widget = self._sidebar.widget
        sidebar_widget.set_margin_top(_SIDEBAR_TOP_INSET)
        sidebar_widget.set_overflow(Gtk.Overflow.HIDDEN)
        self._outer_paned.set_start_child(sidebar_widget)
        self._outer_paned.set_end_child(self._inner_paned)

        self._message_stack = Gtk.Stack()
        # Minimum width for the list column; paned position owns the actual width.
        self._message_stack.set_size_request(MIN_MESSAGE_LIST_WIDTH, -1)
        self._message_stack.set_hexpand(True)
        self._message_stack.set_overflow(Gtk.Overflow.HIDDEN)
        self._message_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._message_stack.set_transition_duration(150)

        # FILL + hexpand so WrappingLabel (0 natural width) gets the list pane,
        # not the 48px icon. CENTER on the box collapses the note (#293).
        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.CENTER,
        )
        loading_box.set_hexpand(True)
        loading_box.set_margin_start(24)
        loading_box.set_margin_end(24)
        self._message_loading_spinner = Gtk.Spinner()
        self._message_loading_spinner.set_size_request(32, 32)
        self._message_loading_spinner.set_halign(Gtk.Align.CENTER)
        loading_box.append(self._message_loading_spinner)
        self._message_loading_progress = Gtk.ProgressBar()
        self._message_loading_progress.set_show_text(False)
        self._message_loading_progress.set_visible(False)
        self._message_loading_progress.set_halign(Gtk.Align.FILL)
        self._message_loading_progress.set_hexpand(True)
        loading_box.append(self._message_loading_progress)
        self._message_loading_label = WrappingLabel(
            label="Loading Messages…",
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD,
        )
        self._message_loading_label.set_halign(Gtk.Align.FILL)
        self._message_loading_label.set_hexpand(True)
        self._message_loading_label.set_justify(Gtk.Justification.CENTER)
        self._message_loading_label.add_css_class("dim-label")
        loading_box.append(self._message_loading_label)
        self._message_stack.add_named(loading_box, "loading")

        self._message_list_view = VirtualMessageList()
        self._message_scroll = self._message_list_view
        self._message_list_view.set_size_request(MIN_MESSAGE_LIST_WIDTH, -1)
        self._message_list_view.set_callbacks(
            on_selection_changed=self._on_message_list_selection_changed,
            on_item_activated=self._on_message_list_item_activated,
            on_item_pressed=self._on_message_list_item_pressed,
            on_item_context_menu=self._on_message_list_context_menu,
            on_near_end=self._on_message_list_near_end,
        )
        self._setup_message_shortcuts()

        self._message_list_view.set_hexpand(True)
        self._message_stack.add_named(self._message_list_view, "list")

        empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.CENTER,
        )
        empty_box.set_hexpand(True)
        empty_box.set_margin_start(24)
        empty_box.set_margin_end(24)
        empty_icon = Gtk.Image.new_from_icon_name("mail-read-symbolic")
        empty_icon.set_pixel_size(48)
        empty_icon.set_halign(Gtk.Align.CENTER)
        empty_icon.add_css_class("dim-label")
        empty_box.append(empty_icon)
        self._message_empty_label = WrappingLabel(
            label="No Messages",
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD,
        )
        self._message_empty_label.set_halign(Gtk.Align.FILL)
        self._message_empty_label.set_hexpand(True)
        self._message_empty_label.set_justify(Gtk.Justification.CENTER)
        self._message_empty_label.add_css_class("dim-label")
        empty_box.append(self._message_empty_label)
        self._message_stack.add_named(empty_box, "empty")

        error_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.FILL,
            valign=Gtk.Align.CENTER,
        )
        error_box.set_hexpand(True)
        error_box.set_margin_start(24)
        error_box.set_margin_end(24)
        error_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        error_icon.set_pixel_size(48)
        error_icon.set_halign(Gtk.Align.CENTER)
        error_icon.add_css_class("warning")
        error_box.append(error_icon)
        self._message_error_label = WrappingLabel(
            label="",
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._message_error_label.set_halign(Gtk.Align.FILL)
        self._message_error_label.set_hexpand(True)
        self._message_error_label.set_justify(Gtk.Justification.CENTER)
        error_box.append(self._message_error_label)
        retry_btn = Gtk.Button(label="Try Again")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", self._on_refresh)
        error_box.append(retry_btn)
        self._message_stack.add_named(error_box, "error")

        self._message_stack.set_visible_child_name("list")

        self._reader_pane = MessageReaderPane(
            on_reply=self._on_reply_clicked,
            on_reply_all=self._on_reply_all_clicked,
            on_forward=self._on_forward_clicked,
            on_unsubscribe=self._on_unsubscribe_clicked,
            on_add_to_calendar=self._on_add_to_calendar_clicked,
            on_attachment_clicked=self._on_reader_attachment_clicked,
            on_attachment_context_menu=self._on_reader_attachment_context_menu,
            on_open_uri=self._open_uri_externally,
            on_new_message_to=self._on_new_message_to_address,
            on_search_messages_from=self._search_messages_from_address,
            can_search_messages=lambda: self._header_search_entry.get_sensitive(),
        )
        self._reader_pane.set_hexpand(True)
        self._reader_pane.set_margin_start(16)
        self._reader_pane.set_margin_end(16)
        self._reader_pane.set_margin_top(_SIDEBAR_TOP_INSET)

        self._inner_paned.set_start_child(self._message_stack)
        self._inner_paned.set_end_child(self._reader_pane)

        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._on_app_dark_changed)

        self._outer_paned.connect("notify::position", self._on_pane_position_changed)
        self._inner_paned.connect("notify::position", self._on_pane_position_changed)
        self._outer_paned.connect("map", self._on_outer_paned_map)
        GLib.idle_add(self._restore_pane_widths)

        self._status_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_start=12,
            margin_end=12,
            margin_top=6,
            margin_bottom=6,
        )
        self._status_progress = Gtk.ProgressBar()
        self._status_progress.set_show_text(False)
        self._status_progress.add_css_class("status-search-progress")
        self._status_progress.set_size_request(140, -1)
        self._status_progress.set_valign(Gtk.Align.CENTER)
        self._status_progress.set_vexpand(False)
        self._status_progress.set_visible(False)
        self._status_bar.append(self._status_progress)
        self._status = Gtk.Label(label="", xalign=0)
        self._status.add_css_class("dim-label")
        self._status.set_ellipsize(3)
        self._status.set_hexpand(True)
        self._status.set_valign(Gtk.Align.CENTER)
        self._status.set_vexpand(False)
        self._status_bar.append(self._status)
        self._stop_sending_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._stop_sending_box.set_valign(Gtk.Align.CENTER)
        self._stop_sending_box.set_visible(False)
        self._stop_sending_count = Gtk.Label(label="")
        self._stop_sending_count.add_css_class("dim-label")
        self._stop_sending_count.set_valign(Gtk.Align.CENTER)
        self._stop_sending_box.append(self._stop_sending_count)
        self._status_send_now_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self._status_send_now_btn.set_tooltip_text(format_status_send_now_tooltip(1))
        self._status_send_now_btn.add_css_class("flat")
        self._status_send_now_btn.set_valign(Gtk.Align.CENTER)
        self._status_send_now_btn.connect("clicked", self._on_status_send_now_clicked)
        self._stop_sending_box.append(self._status_send_now_btn)
        self._stop_sending_btn = Gtk.Button(icon_name="process-stop-symbolic")
        self._stop_sending_btn.set_tooltip_text("Stop Sending")
        self._stop_sending_btn.add_css_class("flat")
        self._stop_sending_btn.set_valign(Gtk.Align.CENTER)
        self._stop_sending_btn.set_margin_start(4)
        self._stop_sending_btn.connect("clicked", self._on_stop_sending_clicked)
        self._stop_sending_box.append(self._stop_sending_btn)
        self._status_bar.append(self._stop_sending_box)
        outer.append(self._status_bar)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)
        self._toast_overlay.set_child(outer)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._toast_overlay)
        self.set_content(toolbar_view)

        self._setup_attachment_menu()
        self._setup_message_menu()
        self._setup_undo_action()
        self._setup_compose_action()
        self._setup_delete_shortcut()
        self._setup_search_shortcuts()
        self._setup_send_queue_flush()
        self._mail.offline_sync.add_progress_callback(self._on_offline_sync_progress)
        self._clear_reader()

    def _setup_send_queue_flush(self) -> None:
        self._mail.set_network_available(self._network_available)
        self._sidebar.set_network_available(self._network_available)
        monitor = Gio.NetworkMonitor.get_default()
        monitor.connect("notify::network-available", self._on_network_available_changed)
        self._refresh_status_display()
        GLib.timeout_add_seconds(2, self._flush_send_queue_on_startup)

    def _flush_send_queue_on_startup(self) -> bool:
        self._flush_send_queue_idle()
        self._flush_operation_queue_idle()
        self._flush_draft_queue_idle()
        self._send_delay_scheduler.reschedule_all()
        self._update_stop_sending_button()
        return False

    def _on_network_available_changed(self, monitor: Gio.NetworkMonitor, *_args) -> None:
        online = monitor.get_network_available()
        if online == self._network_available:
            return
        self._network_available = online
        self._mail.set_network_available(online)
        self._sidebar.set_network_available(online)
        self._refresh_status_display()
        if online:
            self._mail.go_online_sync()
            self._flush_send_queue_idle()
            self._flush_operation_queue_idle()
            self._flush_draft_queue_idle()
            self._mail.schedule_offline_body_sync()
            if self._current_account and self._current_folder:
                if self._account_server_sync_enabled(self._current_account.uid):
                    self._load_messages(
                        self._current_account.uid,
                        self._current_folder,
                        sync=True,
                    )
            else:
                GLib.idle_add(self._reload_sidebar)
        elif self._current_account and self._current_folder:
            self._load_messages(
                self._current_account.uid,
                self._current_folder,
                sync=False,
            )

    def _flush_send_queue_idle(self, *, force: bool = False) -> bool:
        get_mail_io_thread().submit(self._flush_send_queue_worker, force=force)
        return False

    def _flush_send_queue_worker(self, *, force: bool = False) -> None:
        try:
            result = self._mail.flush_send_queue(force=force)
        except Exception:
            log.exception("Failed to flush outbound send queue")
            return
        GLib.idle_add(self._on_send_queue_flushed, result)

    def _flush_operation_queue_idle(self) -> bool:
        get_mail_io_thread().submit(self._flush_operation_queue_worker)
        return False

    def _flush_operation_queue_worker(self) -> None:
        try:
            flushed = self._mail.flush_operation_queue()
        except Exception:
            log.exception("Failed to flush queued mail operations")
            return
        if flushed <= 0:
            return
        GLib.idle_add(self._on_operation_queue_flushed, flushed)

    def _on_operation_queue_flushed(self, flushed: int) -> bool:
        self._refresh_status_display()
        if flushed <= 0:
            return False
        if flushed == 1:
            self._set_status("Synced 1 queued action")
        else:
            self._set_status(f"Synced {flushed} queued actions")
        return False

    def _flush_draft_queue_idle(self) -> bool:
        get_mail_io_thread().submit(self._flush_draft_queue_worker)
        return False

    def _flush_draft_queue_worker(self) -> None:
        try:
            flushed = self._mail.flush_draft_queue()
        except Exception:
            log.exception("Failed to flush queued drafts")
            return
        if flushed <= 0:
            return
        GLib.idle_add(self._on_draft_queue_flushed, flushed)

    def _on_draft_queue_flushed(self, flushed: int) -> bool:
        self._refresh_status_display()
        if flushed <= 0:
            return False
        if flushed == 1:
            self._set_status("Synced 1 queued draft to Drafts")
        else:
            self._set_status(f"Synced {flushed} queued drafts to Drafts")
        return False

    def _on_send_queue_flushed(self, result) -> bool:
        from post.mail.eds import FlushSendQueueResult

        if not isinstance(result, FlushSendQueueResult):
            result = FlushSendQueueResult(sent=int(result))
        self._on_outbox_changed()
        if result.failed_account_uid:
            self._sidebar.refresh_account_online_marker(result.failed_account_uid)
        if result.error_message:
            show_error_toast(self, result.error_message)
            self._set_status(result.error_message)
            return False
        if result.sent <= 0:
            return False
        if result.sent == 1:
            self._set_status("Sent 1 queued message")
        else:
            self._set_status(f"Sent {result.sent} queued messages")
        return False

    def _on_outbox_changed(self) -> None:
        self._sidebar.refresh_outbox_rows()
        self._update_stop_sending_button()
        self._refresh_status_display()
        if (
            self._current_account
            and is_post_outbox_folder(self._current_folder)
        ):
            self._load_messages(self._current_account.uid, POST_OUTBOX_FOLDER)

    def _update_stop_sending_button(self) -> None:
        count = sum(
            1
            for _queue_id, message in list_queued_outbound_messages()
            if has_pending_send_delay(message)
        )
        if count:
            self._stop_sending_count.set_label(str(count))
            self._status_send_now_btn.set_tooltip_text(
                format_status_send_now_tooltip(count)
            )
            self._stop_sending_box.set_visible(True)
        else:
            self._stop_sending_count.set_label("")
            self._stop_sending_box.set_visible(False)
        sensitive = not self._stop_sending_in_flight
        self._status_send_now_btn.set_sensitive(sensitive)
        self._stop_sending_btn.set_sensitive(sensitive)
        self._sync_send_delay_status()

    def _send_delay_status_text(self) -> str:
        messages = [
            message for _queue_id, message in list_queued_outbound_messages()
        ]
        send_after = soonest_pending_send_after(messages)
        if send_after is None:
            return ""
        remaining = remaining_send_delay_seconds(send_after)
        if remaining <= 0:
            return ""
        return format_send_delay_status(remaining)

    def _sync_send_delay_status(self) -> None:
        text = self._send_delay_status_text()
        if text != self._send_delay_status:
            self._send_delay_status = text
            self._refresh_status_display()
        if text:
            self._start_send_delay_tick()
        else:
            self._stop_send_delay_tick()

    def _start_send_delay_tick(self) -> None:
        if self._send_delay_tick_id is not None or self._is_closing:
            return
        self._send_delay_tick_id = GLib.timeout_add_seconds(
            1, self._on_send_delay_tick
        )

    def _stop_send_delay_tick(self) -> None:
        timer_id = self._send_delay_tick_id
        if timer_id is None:
            return
        GLib.source_remove(timer_id)
        self._send_delay_tick_id = None

    def _on_send_delay_tick(self) -> bool:
        if self._is_closing:
            self._send_delay_tick_id = None
            self._send_delay_status = ""
            return False
        text = self._send_delay_status_text()
        if text != self._send_delay_status:
            self._send_delay_status = text
            self._refresh_status_display()
        if not text:
            self._send_delay_tick_id = None
            return False
        return True

    def _install_message_list_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(_MESSAGE_LIST_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _on_window_size_changed(self, *_args) -> None:
        if self.is_maximized():
            return
        width = self.get_width()
        height = self.get_height()
        if width > 0 and height > 0:
            self._last_normal_width = width
            self._last_normal_height = height

    def _restore_pane_widths(self) -> bool:
        self._pane_widths_ready = False
        self._sidebar_width = max(MIN_SIDEBAR_WIDTH, int(self._sidebar_width))
        self._message_list_width = max(
            MIN_MESSAGE_LIST_WIDTH, int(self._message_list_width)
        )
        self._outer_paned.set_position(self._sidebar_width)
        self._inner_paned.set_position(self._message_list_width)
        self._pane_widths_ready = True
        return GLib.SOURCE_REMOVE

    def _on_outer_paned_map(self, *_args) -> None:
        if self._pane_map_restored:
            return
        self._pane_map_restored = True
        self._restore_pane_widths()

    def _on_pane_position_changed(self, paned: Gtk.Paned, *_args) -> None:
        if not self._pane_widths_ready:
            return
        position = int(paned.get_position())
        if paned is self._outer_paned:
            if position < MIN_SIDEBAR_WIDTH:
                paned.set_position(MIN_SIDEBAR_WIDTH)
                return
            self._sidebar_width = position
        elif paned is self._inner_paned:
            if position < MIN_MESSAGE_LIST_WIDTH:
                paned.set_position(MIN_MESSAGE_LIST_WIDTH)
                return
            self._message_list_width = position
        else:
            return
        self._persist_window_state()

    def _persist_window_state(self) -> None:
        if self.is_maximized():
            width = self._last_normal_width
            height = self._last_normal_height
        else:
            width = self.get_width() or self._last_normal_width
            height = self.get_height() or self._last_normal_height
            if width > 0 and height > 0:
                self._last_normal_width = width
                self._last_normal_height = height
        if self._pane_widths_ready:
            outer_pos = int(self._outer_paned.get_position())
            inner_pos = int(self._inner_paned.get_position())
            if outer_pos > 0:
                self._sidebar_width = outer_pos
            if inner_pos > 0:
                self._message_list_width = inner_pos
        set_window_state(
            width=width,
            height=height,
            maximized=self.is_maximized(),
            sidebar_width=self._sidebar_width,
            message_list_width=self._message_list_width,
        )

    def _on_close_request(self, *_args) -> bool:
        if self._mail.outbound_sends_pending():
            self._set_status(
                "Sending message… Post will close when sending finishes."
            )
            show_toast(
                self,
                "A message is still sending. Post will close when sending finishes.",
                timeout=8,
            )
            if not self._close_after_outbound_send:
                self._close_after_outbound_send = True
                self._mail.when_outbound_sends_complete(
                    self._continue_close_after_outbound_send
                )
            return True

        if self._close_after_outbound_send:
            return True

        if self._mail.folder_transfers_pending():
            self._set_status(
                "Moving messages… Post will close when the move finishes."
            )
            show_toast(
                self,
                "A move is still in progress. Post will close when it finishes.",
                timeout=8,
            )
            if not self._close_after_folder_transfer:
                self._close_after_folder_transfer = True
                self._mail.when_folder_transfers_complete(
                    self._continue_close_after_folder_transfer
                )
            return True

        if self._close_after_folder_transfer:
            return True

        pending_delayed = list_pending_delayed_outbound_messages()
        if pending_delayed and self._delayed_send_close_dialog is None:
            self._prompt_send_delayed_before_close(pending_delayed)
            return True

        GLib.idle_add(self._destroy_after_close_cleanup)
        return True

    def _abort_inflight_search(self) -> None:
        if self._is_closing:
            return
        self._is_closing = True
        self._messages_load_generation += 1
        self._search_query = None
        self._mail.cancel_folder_search()

    def _prompt_send_delayed_before_close(
        self,
        pending: list[tuple[str, QueuedOutboundMessage]],
    ) -> None:
        count = len(pending)
        if count == 1:
            heading = "Send delayed message?"
        else:
            heading = f"Send {count} delayed messages?"
        if self._network_available:
            body = (
                "These messages are waiting for the send delay. "
                "Send them now before quitting?"
            )
        else:
            body = (
                "These messages are waiting for the send delay. "
                "They will stay in the outbox until Post is online."
            )
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("leave", "Leave in Outbox")
        if self._network_available:
            dialog.add_response("send", "Send Now")
            dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("send")
        else:
            dialog.set_default_response("leave")
        dialog.set_response_appearance("leave", Adw.ResponseAppearance.DESTRUCTIVE)
        self._delayed_send_close_dialog = dialog
        dialog.connect("response", self._on_delayed_send_close_response)
        dialog.present(self)

    def _on_delayed_send_close_response(
        self, dialog: Adw.AlertDialog, response: str
    ) -> None:
        self._delayed_send_close_dialog = None
        if response == "cancel":
            return
        if response == "leave":
            GLib.idle_add(self._destroy_after_close_cleanup)
            return
        if response == "send":
            self._send_delay_scheduler.cancel_all()
            self._set_status(
                "Sending message… Post will close when sending finishes."
            )
            self._close_after_outbound_send = True

            def worker() -> None:
                try:
                    self._mail.flush_send_queue(force=True)
                except Exception:
                    log.exception("Failed to send delayed messages before quit")
                GLib.idle_add(self._continue_close_after_outbound_send)

            get_mail_io_thread().submit(worker)

    def _continue_close_after_outbound_send(self) -> None:
        self._close_after_outbound_send = False
        # A move may have started while we waited for send.
        if self._mail.folder_transfers_pending():
            self._on_close_request()
            return
        GLib.idle_add(self._destroy_after_close_cleanup)

    def _continue_close_after_folder_transfer(self) -> None:
        self._close_after_folder_transfer = False
        if self._mail.outbound_sends_pending():
            self._on_close_request()
            return
        GLib.idle_add(self._destroy_after_close_cleanup)

    def _destroy_after_close_cleanup(self) -> bool:
        self._abort_inflight_search()
        self._finish_close()
        self._close_auxiliary_windows()
        application = self.get_application()
        self.destroy()
        if application is not None:
            application.quit()
        return False

    def _close_auxiliary_windows(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.destroy()
            self._settings_dialog = None
        for window in list(self._compose_windows):
            window.force_close()
        for window in list(self._reader_windows):
            window.destroy()

    def _finish_close(self) -> bool:
        self._stop_send_delay_tick()
        self._stop_sync_watcher()
        try:
            self._mail.shutdown_sync()
        except Exception:
            log.exception("Failed to flush mail changes on exit")
        self._persist_window_state()
        return False

    def _setup_delete_shortcut(self) -> None:
        for widget in (self, self._message_list_view.list_view):
            controller = Gtk.EventControllerKey()
            controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            controller.connect("key-pressed", self._on_delete_key_pressed)
            widget.add_controller(controller)

    def _on_delete_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        # Mac keyboards send BackSpace for the key labeled "delete"; PC Delete is
        # KEY_Delete / KEY_KP_Delete (often Fn+Delete on Mac in a VM).
        if keyval not in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace):
            return False
        focus = self.get_focus()
        if isinstance(focus, Gtk.Editable):
            return False
        return self._on_delete_shortcut(self)

    def _on_delete_shortcut(
        self,
        _widget: Gtk.Widget,
        _args: GLib.Variant | None = None,
    ) -> bool:
        if not self._message_list_view.get_selected_uids():
            return False
        if not self._current_account or not self._current_folder:
            return False
        state = self._sidebar.get_move_menu_state(
            self._current_account.uid, self._current_folder
        )
        if not state.get("can_trash"):
            return False
        self._move_selected_messages("trash")
        return True

    def _setup_search_shortcuts(self) -> None:
        focus_action = Gio.SimpleAction.new("focus-search", None)
        focus_action.connect("activate", self._on_focus_search_action)
        self.add_action(focus_action)

        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.focus-search", ["<Control>f"])

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_type_to_search_key_pressed)
        self.add_controller(controller)

    def _focus_search_entry(self, *, select_all: bool = False) -> bool:
        if not self._header_search_entry.get_sensitive():
            return False
        self._header_search_entry.grab_focus()
        if select_all:
            self._header_search_entry.select_region(0, -1)
        return True

    def _on_focus_search_action(self, *_args) -> None:
        self._focus_search_entry(select_all=True)

    @staticmethod
    def _typing_targets_search(
        keyval: int, state: Gdk.ModifierType, focus: Gtk.Widget | None
    ) -> str | None:
        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
            | Gdk.ModifierType.META_MASK
        ):
            return None
        if isinstance(focus, Gtk.Editable):
            return None
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape, Gdk.KEY_Tab):
            return None
        if keyval == Gdk.KEY_space:
            return " "
        unicode_char = Gdk.keyval_to_unicode(keyval)
        if unicode_char == 0 or not chr(unicode_char).isprintable():
            return None
        return chr(unicode_char)

    def _on_type_to_search_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        focus = self.get_focus()
        if focus is self._header_search_entry:
            return False
        if not self._header_search_entry.get_sensitive():
            return False
        if self._message_list_view.get_selected_uids():
            return False

        char = self._typing_targets_search(keyval, state, focus)
        if char is None:
            return False

        if not self._focus_search_entry():
            return False

        self._search_entry_updating = True
        self._header_search_entry.set_text(char)
        self._search_entry_updating = False
        self._header_search_entry.set_position(-1)
        self._apply_search_from_entry()
        return True

    def _set_message_actions_sensitive(self, sensitive: bool) -> None:
        self._reader_pane.set_actions_sensitive(sensitive)

    def _reader_message_flags(self) -> dict:
        if self._current_message_uid is None:
            return {}
        return self._message_flags_for_uid(self._current_message_uid)

    @staticmethod
    def _apply_toggle_button_presentation(
        button: Gtk.Button, state: dict[str, Any]
    ) -> None:
        button.set_icon_name(state["icon"])
        button.set_tooltip_text(state["tooltip"])
        if state["styled_action"]:
            button.add_css_class(state["action_class"])
        else:
            button.remove_css_class(state["action_class"])

    def _on_header_read_toggle_clicked(self, *_args) -> None:
        selected = self._message_list_view.get_selected_uids()
        if not selected:
            return
        seen = uniform_bool_state(
            self._message_seen_states_for_uids(selected, self._message_flags_for_uid)
        )
        if seen is None:
            return
        self._set_message_flags("seen", seen=not seen, uids=selected)

    def _on_header_flag_toggle_clicked(self, *_args) -> None:
        selected = self._message_list_view.get_selected_uids()
        if not selected:
            return
        flagged = uniform_bool_state(
            self._message_flagged_states_for_uids(
                selected, self._message_flags_for_uid
            )
        )
        if flagged is None:
            return
        self._set_message_flags("flagged", flagged=not flagged, uids=selected)

    def _setup_compose_action(self) -> None:
        compose_action = Gio.SimpleAction.new("compose-new", None)
        compose_action.connect("activate", self._on_compose_new_action)
        self.add_action(compose_action)

        reply_action = Gio.SimpleAction.new("compose-reply", None)
        reply_action.connect("activate", self._on_reply_action)
        self.add_action(reply_action)

        reply_all_action = Gio.SimpleAction.new("compose-reply-all", None)
        reply_all_action.connect("activate", self._on_reply_all_action)
        self.add_action(reply_all_action)

        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.compose-new", ["<Control>n"])
            application.set_accels_for_action("win.compose-reply-all", ["<Control><Shift>r"])

    def _on_compose_new_action(self, *_args) -> None:
        self._open_compose_new()

    def _on_compose_new_clicked(self, *_args) -> None:
        self._open_compose_new()

    def _on_header_trash_clicked(self, *_args) -> None:
        self._move_selected_messages("trash")

    def _on_header_archive_clicked(self, *_args) -> None:
        self._move_selected_messages("archive")

    def _on_settings_clicked(self, *_args) -> None:
        existing = self._settings_dialog
        if existing is not None:
            # #235: never re-present a previously hidden Settings instance.
            if existing.get_mapped():
                existing.present()
                return
            existing.destroy()
            self._settings_dialog = None
        dialog = SettingsWindow(
            parent=self,
            mail=self._mail,
            set_status=self._set_status,
            on_saved=self._reload_sidebar,
            on_load_remote_content_changed=self._on_load_remote_content_changed,
            on_message_appearance_changed=self._on_message_appearance_changed,
            on_offline_body_sync_changed=self._on_offline_body_sync_changed,
        )
        self._settings_dialog = dialog
        dialog.connect("destroy", self._on_settings_closed)
        dialog.present()

    def _on_settings_closed(self, *_args) -> None:
        self._settings_dialog = None

    def _account_server_sync_enabled(self, account_uid: str) -> bool:
        return self._network_available and not account_is_user_offline(account_uid)

    def _on_goa_reauth_requested(self, account_uid: str) -> None:
        """Remember account to reconnect after Online Accounts re-auth."""
        self._pending_goa_reauth.add(account_uid)

    def _on_is_active_changed(self, *_args) -> None:
        """After returning from GOA Settings, drop stale stores and reload."""
        if not self.get_property("is-active"):
            return
        if not self._pending_goa_reauth:
            return
        pending = list(self._pending_goa_reauth)
        self._pending_goa_reauth.clear()
        for account_uid in pending:
            self._reconnect_account_after_goa_reauth(account_uid)

    def _reconnect_account_after_goa_reauth(self, account_uid: str) -> None:
        label = self._sidebar.account_display_label(account_uid)
        self._set_status(f"Reconnecting {label}…")

        def work() -> None:
            self._mail._invalidate_account_connection_unlocked(account_uid)
            GLib.idle_add(self._finish_goa_reauth_reconnect, account_uid)

        get_mail_io_thread().submit_front(work)

    def _finish_goa_reauth_reconnect(self, account_uid: str) -> bool:
        self._on_sidebar_refresh_account(account_uid)
        return False

    def _on_sidebar_refresh_account(self, account_uid: str) -> None:
        if not self._network_available:
            return
        label = self._sidebar.account_display_label(account_uid)
        user_offline = account_is_user_offline(account_uid)
        if user_offline:
            self._set_status(f"{label} · showing cached folders")
        else:
            self._set_status(format_account_refresh_start(label))

        def on_complete(folder_count: int, error: Exception | None) -> None:
            if error is not None:
                self._set_status(format_account_refresh_error(label))
            elif user_offline:
                self._set_status(f"{label} · {folder_count} folders from cache")
            else:
                self._set_status(format_account_refresh_done(label, folder_count))

        self._sidebar.reload_account(account_uid, on_complete=on_complete)

    def _resolved_sync_folder_name(self, account_uid: str, folder_name: str) -> str:
        inbox_name = self._sidebar.inbox_folder_for_account(account_uid)
        if inbox_name and folder_name in (inbox_name, "INBOX"):
            return inbox_name
        return folder_name

    def _is_viewing_folder(self, account_uid: str, folder_name: str) -> bool:
        if (
            self._current_account is None
            or self._current_folder is None
            or self._current_account.uid != account_uid
        ):
            return False
        current = MainWindow._resolved_sync_folder_name(
            self, account_uid, self._current_folder
        )
        resolved = MainWindow._resolved_sync_folder_name(
            self, account_uid, folder_name
        )
        return current == resolved

    def _sync_watcher_current_folder(self) -> None:
        if not self._sync_watcher.running:
            return
        account = self._current_account
        folder = self._current_folder
        if account is None or folder is None:
            self._sync_watcher.set_current_folder(None, None)
            return
        resolved = self._resolved_sync_folder_name(account.uid, folder)
        if resolved != folder:
            self._current_folder = resolved
            self._message_list_view.set_drag_context(account.uid, resolved)
        self._sync_watcher.set_current_folder(account.uid, resolved)

    def _maybe_run_pending_sync_folder_refresh(self) -> None:
        pending = self._pending_sync_folder_refresh
        if pending is None or self._message_list_populating:
            return
        account_uid, folder_name = pending
        self._pending_sync_folder_refresh = None
        if not self._is_viewing_folder(account_uid, folder_name):
            return
        self._sync_current_folder_messages(account_uid, folder_name)

    def _on_sidebar_refresh_folder(self, account_uid: str, folder_name: str) -> None:
        if not self._network_available:
            return
        display = self._sidebar.folder_display_name(account_uid, folder_name)
        user_offline = account_is_user_offline(account_uid)
        sync = not user_offline
        if user_offline:
            self._set_status(f"{display} · showing cached list")
        else:
            self._set_status(format_folder_refresh_start(display))
        if sync:
            self._mail.invalidate_folder_index(account_uid, folder_name)

        if self._is_viewing_folder(account_uid, folder_name):
            self._load_messages(
                account_uid,
                folder_name,
                sync=sync,
                force_sync=sync,
            )
            return

        def on_complete(unread: int, total: int, error: Exception | None) -> None:
            if error is not None:
                self._set_status(format_folder_refresh_error(display))
            elif user_offline:
                self._set_status(f"{display} · {total} messages from cache")
            else:
                self._set_status(format_folder_refresh_done(display, unread, total))

        self._sidebar.refresh_folder_row(
            account_uid, folder_name, on_complete=on_complete
        )

    def _on_sidebar_send_outbox(self) -> None:
        self._flush_send_queue_idle(force=True)

    def _on_sidebar_folder_tree_changed(
        self, account_uid: str, removed_folder: str | None
    ) -> None:
        if (
            removed_folder
            and self._current_account
            and self._current_account.uid == account_uid
            and self._current_folder == removed_folder
        ):
            inbox_name = self._sidebar.inbox_folder_for_account(account_uid)
            if inbox_name and self._current_account:
                self._on_folder_selected(self._current_account, inbox_name)

    def _on_sidebar_folder_contents_changed(
        self, account_uid: str, folder_name: str
    ) -> None:
        self._refresh_folder_view(account_uid, folder_name)

    def _dismiss_bulk_archive_progress_toast(self) -> None:
        toast = self._bulk_archive_progress_toast
        if toast is None:
            return
        self._bulk_archive_progress_toast = None
        toast.dismiss()

    def _on_sidebar_move_started(
        self,
        account_uid: str,
        folder_name: str,
        scope: str = "all",
        progress_label: str = "",
    ) -> None:
        self._clear_move_undo()
        self._suppress_sync_list_reload = (account_uid, folder_name)
        if progress_label:
            self._dismiss_bulk_archive_progress_toast()
            self._bulk_archive_progress_toast = show_toast(
                self,
                progress_label,
                priority=Adw.ToastPriority.HIGH,
                timeout=0,
            )
        if (
            self._current_account is None
            or self._current_folder is None
            or self._current_account.uid != account_uid
            or self._current_folder != folder_name
        ):
            return

        messages = list(self._current_folder_messages or [])
        if scope == "all":
            list_keys = [
                self._message_list_key(message)
                for message in messages
                if self._message_list_key(message)
            ]
            # Clear everything visible even if the folder-message cache is empty.
            self._message_list_view.clear()
            self._current_folder_messages = []
            if self._message_total >= 0:
                self._message_total = 0
        else:
            list_keys = [
                self._message_list_key(message)
                for message in messages
                if self._message_list_key(message)
                and message_matches_bulk_archive_scope(message, scope)
            ]
            if not list_keys:
                return
            self._message_list_view.remove_uids(list_keys)
            moved_keys = set(list_keys)
            self._current_folder_messages = [
                message
                for message in messages
                if self._message_list_key(message) not in moved_keys
            ]
            if self._message_total >= 0:
                self._message_total = max(0, self._message_total - len(list_keys))

        cleared_current = any(
            key == self._current_message_uid for key in list_keys
        ) or (scope == "all" and self._current_message_uid is not None)
        if cleared_current:
            self._clear_reader()
            set_active_message_uid(None)
            self._restore_message_folder = None

        if self._message_list_view.item_count() == 0 and folder_name:
            self._message_empty_label.set_label(f"No Messages in {folder_name}")
            self._message_stack.set_visible_child_name("empty")
        self._update_message_toolbar()

    def _on_sidebar_bulk_archive_finished(
        self,
        account_uid: str,
        folder_name: str,
        result: dict | None,
        status_label: str,
    ) -> None:
        self._dismiss_bulk_archive_progress_toast()
        suppress_key = (account_uid, folder_name)
        archived_count = int((result or {}).get("archived_count") or 0)
        if result is None or archived_count <= 0:
            if self._suppress_sync_list_reload == suppress_key:
                self._suppress_sync_list_reload = None
            if (
                self._current_account
                and self._current_folder
                and self._current_account.uid == account_uid
                and self._current_folder == folder_name
            ):
                self._load_messages(account_uid, folder_name, sync=False)
            return

        self._update_sidebar_from_move_result(account_uid, result)
        if (
            self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
            and self._message_list_view.item_count() == 0
        ):
            self._message_empty_label.set_label(f"No Messages in {folder_name}")
            self._message_stack.set_visible_child_name("empty")

        dest_folder = result.get("destination_folder")
        dest_uids = result.get("destination_uids") or []
        if dest_folder and dest_uids:
            # Arm Undo immediately; present the toast on idle so dismissing the
            # in-progress toast cannot swallow the Undo button (#261).
            self._arm_move_undo(
                account_uid=account_uid,
                source_folder=folder_name,
                dest_folder=dest_folder,
                dest_uids=dest_uids,
            )
            GLib.idle_add(self._show_move_undo_toast, status_label)
            self._set_status(f"{status_label}  ·  Ctrl+Z to undo")
        else:
            # Still confirm success when Graph/Camel omit destination UIDs
            # (Undo requires those UIDs; status-only was easy to miss after
            # optimistic clear — #261).
            GLib.idle_add(
                self._show_bulk_archive_done_toast,
                status_label,
            )
            self._set_status(status_label)

        if self._suppress_sync_list_reload == suppress_key:
            self._suppress_sync_list_reload = None
        self._update_message_toolbar()

    def _on_sidebar_bulk_archive_error(
        self, account_uid: str, folder_name: str, _error: Exception
    ) -> None:
        self._dismiss_bulk_archive_progress_toast()
        suppress_key = (account_uid, folder_name)
        if self._suppress_sync_list_reload == suppress_key:
            self._suppress_sync_list_reload = None
        if (
            self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
        ):
            self._load_messages(account_uid, folder_name, sync=False)

    def _on_sidebar_move_undo_available(
        self,
        account_uid: str,
        source_folder: str,
        result: dict,
        status_label: str,
    ) -> None:
        dest_folder = result.get("destination_folder")
        dest_uids = result.get("destination_uids") or []
        if not dest_folder or not dest_uids:
            return
        self._register_move_undo(
            status_label,
            account_uid=account_uid,
            source_folder=source_folder,
            dest_folder=dest_folder,
            dest_uids=dest_uids,
        )
        self._set_status(f"{status_label}  ·  Ctrl+Z to undo")

    def _on_accounts_loaded(self, account_uids: list[str]) -> None:
        self._sync_watcher.set_accounts(account_uids)
        if any(not account_is_user_offline(uid) for uid in account_uids):
            self._start_sync_watcher()
        else:
            self._stop_sync_watcher()
        self._rebuild_search_scope_dropdown(account_uids)
        self._maybe_show_offline_body_sync_prompt(account_uids)

    def _on_initial_folder_load_complete(self) -> None:
        self._mail.schedule_offline_body_sync()

    def _on_folder_tree_ready(self) -> None:
        # Folder tree is up; make sure a searchable folder is selected so the
        # header search bar can enable without waiting for a click (#196).
        if (
            self._current_account is None
            or self._current_folder is None
            or is_post_outbox_folder(self._current_folder)
        ):
            self._sidebar.ensure_folder_selection()
        self._update_search_entry_state()
        # Folder tree cache is now populated; refresh sync watch so inbox uses
        # the real folder name instead of skipping while cache was empty (#153).
        if self._sync_watcher.running:
            self._sync_watcher.set_accounts(self._sidebar.account_uids())
        self._sync_watcher_current_folder()
        # Defer the first all-folder count poll until after message list I/O
        # has had a chance to run (#170).
        if self._sync_watcher.running and self._folder_count_poll_deferred_id is None:
            self._folder_count_poll_deferred_id = GLib.timeout_add_seconds(
                5, self._on_deferred_folder_count_poll
            )

    def _on_deferred_folder_count_poll(self) -> bool:
        self._folder_count_poll_deferred_id = None
        if self._sync_watcher.running:
            self._sidebar.refresh_all_folder_counts()
        return False

    def _remote_sync_account_backends(self) -> frozenset[str]:
        return frozenset({"imap", "imapx", "ews", "microsoft365", "pop3"})

    def _apply_offline_body_sync_to_accounts(
        self, account_uids: list[str], mode: OfflineBodySyncMode
    ) -> None:
        for account_uid in account_uids:
            account = self._mail.get_account(account_uid)
            if account.backend not in self._remote_sync_account_backends():
                continue
            set_account_offline_body_sync(account_uid, mode)
            self._mail.refresh_offline_settings(account_uid)

    def _maybe_show_offline_body_sync_prompt(self, account_uids: list[str]) -> None:
        remote_accounts = [
            uid
            for uid in account_uids
            if self._mail.get_account(uid).backend in self._remote_sync_account_backends()
        ]
        if not should_show_offline_body_sync_prompt(remote_accounts):
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Offline Mail",
            body=(
                "Post can download message bodies from all folders so you can "
                "read mail and search message text while offline. Header search "
                "uses the folder index and does not need this. This uses extra "
                "disk space and network bandwidth."
            ),
        )
        dialog.add_response("not_now", "Not Now")
        dialog.add_response("last_month", "Last Month")
        dialog.add_response("last_year", "Last Year")
        dialog.add_response("all", "Everything")
        dialog.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("last_month")
        dialog.set_close_response("not_now")

        def on_response(_dialog: Adw.MessageDialog, response: str) -> None:
            if response == "not_now":
                set_offline_body_sync_prompt_declined(True)
                return
            mode_by_response = {
                "last_month": OFFLINE_BODY_SYNC_LAST_MONTH,
                "last_year": OFFLINE_BODY_SYNC_LAST_YEAR,
                "all": OFFLINE_BODY_SYNC_ALL,
            }
            mode = mode_by_response.get(response)
            if mode is None:
                return

            def apply_mode() -> bool:
                self._apply_offline_body_sync_to_accounts(remote_accounts, mode)
                return False

            GLib.idle_add(apply_mode)

        dialog.connect("response", on_response)
        dialog.present()

    def _on_offline_sync_progress(self, progress: OfflineSyncProgress | None) -> None:
        def update() -> bool:
            if progress is None or not progress.active:
                self._offline_download_status = ""
            elif self._offline_held_for_load_generation is not None:
                # Interactive folder work holds offline sync; ignore late progress.
                self._offline_download_status = ""
            else:
                self._offline_download_status = offline_cache_status_text(
                    account_label=progress.account_label,
                    folder_name=progress.folder_name or "",
                )
            self._refresh_status_display()
            return False

        GLib.idle_add(update)

    def _hold_offline_sync_for_folder_work(self, load_id: int) -> None:
        """Cancel offline backfill so opening Archive/etc. can use mail I/O (#208)."""
        self._offline_held_for_load_generation = load_id
        self._offline_download_status = ""
        self._mail.hold_offline_body_sync(True)
        self._refresh_status_display()

    def _heavy_folder_still_selected(self, load_id: int) -> bool:
        """True while the open folder is still this heavy-folder load (#208)."""
        if load_id != self._messages_load_generation:
            return False
        account = self._current_account
        folder = self._current_folder
        return (
            account is not None
            and folder is not None
            and is_heavy_folder_name(folder)
        )

    def _release_offline_sync_for_folder_work(self, load_id: int) -> None:
        if self._offline_held_for_load_generation != load_id:
            return
        # Keep holding while the clicked heavy folder stays selected so Gmail
        # (or any other account) offline backfill cannot steal status / mail I/O.
        if self._heavy_folder_still_selected(load_id):
            self._offline_download_status = ""
            self._refresh_status_display()
            return
        self._offline_held_for_load_generation = None
        self._mail.hold_offline_body_sync(False)
        self._sync_watcher_current_folder()

    def _on_offline_body_sync_changed(
        self, account_uid: str, mode: OfflineBodySyncMode
    ) -> None:
        self._mail.refresh_offline_settings(account_uid)

    def _on_account_online_changed(self, account_uid: str, online: bool) -> None:
        self._sidebar.refresh_account_online_marker(account_uid)
        self._sync_watcher.set_accounts(self._sidebar.account_uids())
        if online:
            self._start_sync_watcher()
            if (
                self._current_account
                and self._current_account.uid == account_uid
                and self._current_folder
                and self._network_available
            ):
                self._load_messages(account_uid, self._current_folder, sync=True)
        else:
            if not any(
                not account_is_user_offline(uid)
                for uid in self._sidebar.account_uids()
            ):
                self._stop_sync_watcher()
            if (
                self._current_account
                and self._current_account.uid == account_uid
                and self._current_folder
            ):
                self._load_messages(account_uid, self._current_folder, sync=False)
        label = self._sidebar.account_display_label(account_uid)
        if online:
            self._set_status(f"{label} is online")
        else:
            self._set_status(f"{label} is offline")

    def _start_sync_watcher(self) -> None:
        if not self._sync_watcher.running:
            self._sync_watcher.start()
        self._start_folder_count_poll()
        self._start_open_folder_poll()

    def _stop_sync_watcher(self) -> None:
        self._stop_folder_count_poll()
        self._stop_open_folder_poll()
        if self._sync_watcher.running:
            self._sync_watcher.stop()

    def _start_folder_count_poll(self) -> None:
        if self._folder_count_poll_timer_id is not None:
            return
        self._folder_count_poll_timer_id = GLib.timeout_add_seconds(
            60, self._on_folder_count_poll_tick
        )

    def _stop_folder_count_poll(self) -> None:
        timer_id = self._folder_count_poll_timer_id
        if timer_id is not None:
            GLib.source_remove(timer_id)
            self._folder_count_poll_timer_id = None
        deferred_id = self._folder_count_poll_deferred_id
        if deferred_id is not None:
            GLib.source_remove(deferred_id)
            self._folder_count_poll_deferred_id = None
        # Cancel any in-flight one-folder poll chain.
        self._sidebar.cancel_folder_count_poll()

    def _on_folder_count_poll_tick(self) -> bool:
        if not self._sync_watcher.running:
            self._folder_count_poll_timer_id = None
            return False
        self._sidebar.refresh_all_folder_counts()
        return True

    def _start_open_folder_poll(self) -> None:
        """Poll the open folder on Graph backends that lack live push (#270)."""
        if self._open_folder_poll_timer_id is not None:
            return
        self._open_folder_poll_timer_id = GLib.timeout_add_seconds(
            30, self._on_open_folder_poll_tick
        )

    def _stop_open_folder_poll(self) -> None:
        timer_id = self._open_folder_poll_timer_id
        if timer_id is not None:
            GLib.source_remove(timer_id)
            self._open_folder_poll_timer_id = None

    def _on_open_folder_poll_tick(self) -> bool:
        if not self._sync_watcher.running:
            self._open_folder_poll_timer_id = None
            return False
        account = self._current_account
        folder_name = self._current_folder
        if account is None or folder_name is None:
            return True
        if (account.backend or "").lower() not in FOLLOW_UP_FLAG_BACKENDS:
            return True
        if (
            not self._network_available
            or not self._account_server_sync_enabled(account.uid)
            or is_heavy_folder_name(folder_name)
            or is_post_outbox_folder(folder_name)
            or self._search_query is not None
            or self._message_sync_in_progress
            or self._message_list_populating
        ):
            return True
        self._sync_current_folder_messages(account.uid, folder_name)
        return True

    def _on_sync_folder_changed(self, account_uid: str, folder_name: str) -> None:
        search_trace(
            "sync_folder_changed",
            account=account_uid,
            folder=folder_name,
            current=self._current_folder,
        )
        # Heavy-folder header refresh emits Folder::changed. Invalidating the
        # grow-only index (even after leaving the folder) wipes thousands of
        # headers; a reopen then rebuilds from a shrunk Camel summary and may
        # call prepare_content_refresh (#208).
        if is_heavy_folder_name(folder_name):
            self._sidebar.refresh_folder_row(account_uid, folder_name)
            return
        if (
            self._message_sync_in_progress
            and self._current_account is not None
            and self._current_account.uid == account_uid
            and self._is_viewing_folder(account_uid, folder_name)
        ):
            self._sidebar.refresh_folder_row(account_uid, folder_name)
            return
        self._mail.invalidate_folder_index(account_uid, folder_name)
        self._refresh_folder_view(account_uid, folder_name)

    def _on_sync_folder_tree_changed(self, account_uid: str) -> None:
        self._sidebar.reload_account(account_uid)
        self._on_sidebar_folder_tree_changed(account_uid, None)

    def _refresh_folder_view(self, account_uid: str, folder_name: str) -> None:
        self._sidebar.refresh_folder_row(account_uid, folder_name)
        if self._current_account and self._current_folder and self._is_viewing_folder(
            account_uid, folder_name
        ):
            if self._sync_list_reload_suppressed(account_uid, folder_name):
                return
            if self._folder_messages_visible():
                self._sync_current_folder_messages(account_uid, folder_name)
                return
            self._load_messages(account_uid, folder_name, sync=True)

    def _folder_messages_visible(self) -> bool:
        return (
            self._message_list_view.item_count() > 0
            or bool(self._current_folder_messages)
        )

    def _sync_current_folder_messages(
        self, account_uid: str, folder_name: str
    ) -> None:
        """Refresh the open folder from the server without clearing the list."""
        if (
            self._search_query is not None
            or is_post_outbox_folder(folder_name)
            or not self._account_server_sync_enabled(account_uid)
            or self._message_sync_in_progress
        ):
            return

        load_id = self._messages_load_generation

        def fetch_messages(sync_flag: bool) -> tuple[list[dict], int, int, str]:
            messages, unread, total, source = self._mail.get_folder_messages(
                account_uid,
                folder_name,
                sync=sync_flag,
            )
            return messages, unread, total, source

        self._message_sync_in_progress = True
        self._start_background_message_sync(
            load_id,
            account_uid,
            folder_name,
            fetch_messages,
        )

    def _on_load_remote_content_changed(self, enabled: bool) -> None:
        self._load_remote_content = enabled
        self._reader_pane.refresh_document(allow_remote=enabled)

    def _on_message_appearance_changed(self, appearance: MessageAppearance) -> None:
        self._message_appearance = appearance
        self._reader_pane.refresh_document(message_appearance=appearance)

    def _on_reply_action(self, *_args) -> None:
        self._open_compose_on_message("reply")

    def _on_reply_all_action(self, *_args) -> None:
        self._open_compose_on_message("reply-all")

    def _on_reply_clicked(self, *_args) -> None:
        self._open_compose_on_message("reply")

    def _on_reply_all_clicked(self, *_args) -> None:
        self._open_compose_on_message("reply-all")

    def _on_forward_clicked(self, *_args) -> None:
        self._open_compose_on_message("forward")

    @staticmethod
    def _unsubscribe_action_from_message(msg: dict | None) -> dict[str, str] | None:
        if msg is None:
            return None
        action = msg.get("unsubscribe")
        if not isinstance(action, dict):
            return None
        kind = action.get("kind")
        url = action.get("url")
        if kind in ("post", "open") and isinstance(url, str) and url:
            return {"kind": kind, "url": url}
        return None

    def _unsubscribe_action_for_list_key(self, list_key: str) -> dict[str, str] | None:
        if list_key == self._current_message_uid:
            action = self._unsubscribe_action_from_message(self._current_message)
            if action is not None:
                return action
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return None
        account_uid, folder_name, message_uid = location
        for window in self._reader_windows:
            if (
                window.account_uid == account_uid
                and window.folder_name == folder_name
                and window.message_uid == message_uid
            ):
                return self._unsubscribe_action_from_message(window.current_message)
        return None

    @staticmethod
    def _calendar_invite_from_message(msg: dict | None) -> dict | None:
        if msg is None:
            return None
        invite = msg.get("calendar_invite")
        return dict(invite) if isinstance(invite, dict) else None

    def _calendar_invite_for_list_key(self, list_key: str) -> dict | None:
        if list_key == self._current_message_uid:
            invite = self._calendar_invite_from_message(self._current_message)
            if invite is not None:
                return invite
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return None
        account_uid, folder_name, message_uid = location
        for window in self._reader_windows:
            if (
                window.account_uid == account_uid
                and window.folder_name == folder_name
                and window.message_uid == message_uid
            ):
                return self._calendar_invite_from_message(window.current_message)
        return None

    def _on_unsubscribe_clicked(self, action: dict[str, str]) -> None:
        self._run_unsubscribe_action(action, list_key=self._current_message_uid)

    def _on_add_to_calendar_clicked(self, invite: dict) -> None:
        from post.calendar_dialog import present_add_to_calendar
        from post.mail.io_thread import get_mail_io_thread

        present_add_to_calendar(
            self,
            invite,
            on_success=lambda label: show_toast(self, f"Added to {label}"),
            on_error=lambda message: show_error_toast(self, message),
            run_async=lambda worker: get_mail_io_thread().submit(worker),
        )

    def _can_archive_list_key(self, list_key: str | None) -> bool:
        if not list_key:
            return False
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return False
        account_uid, folder_name, _message_uid = location
        return bool(
            self._sidebar.get_move_menu_state(account_uid, folder_name).get(
                "can_archive"
            )
        )

    def _archive_after_unsubscribe(self, list_key: str | None) -> None:
        if not list_key or not self._can_archive_list_key(list_key):
            return
        self._move_messages("archive", [list_key])

    def _run_unsubscribe_action(
        self,
        action: dict[str, str],
        *,
        list_key: str | None = None,
    ) -> None:
        kind = action["kind"]
        url = action["url"]
        if kind == "open":
            # Launch first so the click gesture still counts for focus activation.
            self._open_uri_externally(url)
            if url.lower().startswith("mailto:"):
                show_toast(
                    self,
                    "Opening unsubscribe email…",
                    priority=Adw.ToastPriority.HIGH,
                )
            else:
                show_toast(
                    self,
                    "Opening unsubscribe page in your browser…",
                    priority=Adw.ToastPriority.HIGH,
                )
            self._archive_after_unsubscribe(list_key)
            return
        if kind != "post":
            return
        will_archive = self._can_archive_list_key(list_key)
        body = (
            "Send a one-click unsubscribe request and archive this message?"
            if will_archive
            else "Send a one-click unsubscribe request for this mailing list?"
        )
        if not confirm_action(
            self,
            heading="Unsubscribe?",
            body=body,
            confirm_label="Unsubscribe",
        ):
            return

        def worker() -> None:
            error: Exception | None = None
            try:
                perform_one_click_unsubscribe(url)
            except Exception as exc:
                log.exception("One-click unsubscribe failed")
                error = exc
            GLib.idle_add(self._on_one_click_unsubscribe_done, error, list_key)

        get_mail_io_thread().submit(worker)

    def _on_one_click_unsubscribe_done(
        self,
        error: Exception | None,
        list_key: str | None = None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Unsubscribe failed: {error}")
        else:
            show_toast(self, "Unsubscribe request sent")
            self._archive_after_unsubscribe(list_key)
        return False

    def _compose_account(self) -> MailAccount | None:
        if self._current_account is not None:
            return self._current_account
        accounts = self._mail.list_accounts()
        return MailService.pick_default_account(accounts)

    def _open_compose_new(self) -> None:
        sendable = self._mail.list_sendable_accounts()
        if not sendable:
            self._set_status("No mail account configured for sending")
            return
        account = self._compose_account()
        if account is None or not account.can_send:
            account = MailService.pick_default_account(sendable) or sendable[0]
        self._present_compose_window(account, mode="new")

    def open_compose_mailto(self, uri: str) -> None:
        """Open a new compose window prefilled from a mailto: URI."""
        try:
            mailto = parse_mailto_uri(uri)
        except ValueError:
            self._set_status("Could not open mailto link")
            return
        sendable = self._mail.list_sendable_accounts()
        if not sendable:
            self._set_status("No mail account configured for sending")
            return
        account = self._compose_account()
        if account is None or not account.can_send:
            account = MailService.pick_default_account(sendable) or sendable[0]
        # Present compose last so the main window does not cover it.
        self._present_compose_window(account, mode="new", mailto=mailto)

    def _on_new_message_to_address(self, email: str) -> None:
        address = (email or "").strip()
        if not address:
            return
        self.open_compose_mailto(f"mailto:{address}")

    def _search_messages_from_address(self, email: str) -> None:
        """Search for messages from *email* using the current search scope."""
        query = format_from_search_query(email)
        if not query:
            return
        if not self._header_search_entry.get_sensitive():
            return
        self.present()
        self._search_entry_updating = True
        self._header_search_entry.set_text(query)
        self._search_entry_updating = False
        self._header_search_entry.set_position(-1)
        self._apply_search_from_entry()

    def _open_compose_on_message(self, mode: str) -> None:
        if not self._current_message_uid:
            prompt = "Select a message to forward" if mode == "forward" else "Select a message to reply"
            self._set_status(prompt)
            return
        list_key = self._current_message_uid
        location = self._message_location_for_list_key(list_key)
        if location is None:
            prompt = "Select a message to forward" if mode == "forward" else "Select a message to reply"
            self._set_status(prompt)
            return
        account_uid, folder_name, message_uid = location
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            self._set_status("Selected account is no longer available")
            return
        if not self._mail.list_sendable_accounts():
            self._set_status("No mail account configured for sending")
            return
        if not account.can_send:
            self._set_status("Selected account has no mail transport configured")
            return
        if self._current_message is not None:
            self._present_compose_window(
                account,
                mode=mode,
                reply_to=self._current_message,
                source_folder_name=folder_name,
                source_message_uid=message_uid,
            )
            return

        preparing = "Preparing forward…" if mode == "forward" else "Preparing reply…"
        self._set_status(preparing)

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                msg = self._mail.read_message(account_uid, folder_name, message_uid)
            except MessageNotAvailableError as exc:
                log.warning(
                    "Message %s no longer available in %r",
                    message_uid,
                    folder_name,
                )
                error = exc
            except Exception as exc:
                log.exception("Failed to load message for compose")
                error = exc
            GLib.idle_add(
                self._on_compose_message_loaded,
                account,
                msg,
                error,
                mode,
                folder_name,
                message_uid,
            )

        get_mail_io_thread().submit(worker)

    def _on_compose_message_loaded(
        self,
        account: MailAccount,
        msg: dict | None,
        error: Exception | None,
        mode: str,
        folder_name: str,
        message_uid: str,
    ) -> bool:
        if error is not None:
            if isinstance(error, MessageNotAvailableError):
                if error.reason == MessageUnavailableReason.VANISHED:
                    self._remove_vanished_message(error.message_uid)
                show_error_toast(self, error.user_message())
                return False
            action = "forward" if mode == "forward" else "reply"
            show_error_toast(self, f"Could not prepare {action}: {error}")
            return False
        if msg is None:
            return False
        self._current_message = msg
        self._set_message_actions_sensitive(True)
        self._present_compose_window(
            account,
            mode=mode,
            reply_to=msg,
            source_folder_name=folder_name,
            source_message_uid=message_uid,
        )
        return False

    def _present_compose_window(
        self,
        account: MailAccount,
        *,
        mode: str,
        reply_to: dict | None = None,
        draft_folder_name: str | None = None,
        draft_message_uid: str | None = None,
        draft_message: dict | None = None,
        source_folder_name: str | None = None,
        source_message_uid: str | None = None,
        outbox_queue_id: str | None = None,
        mailto: MailtoCompose | None = None,
    ) -> None:
        window = ComposeWindow(
            parent=self,
            mail=self._mail,
            account=account,
            set_status=self._set_status,
            on_outbox_changed=self._on_outbox_changed,
            on_draft_saved=self._on_draft_saved,
            on_draft_save_started=self._on_draft_save_started,
            on_delayed_send=self._send_delay_scheduler.schedule_item,
            mode=mode,  # type: ignore[arg-type]
            reply_to=reply_to,
            draft_folder_name=draft_folder_name,
            draft_message_uid=draft_message_uid,
            draft_message=draft_message,
            source_folder_name=source_folder_name,
            source_message_uid=source_message_uid,
            outbox_queue_id=outbox_queue_id,
            mailto=mailto,
        )
        self._compose_windows.append(window)
        window.connect(
            "destroy",
            lambda *_args, w=window: self._compose_windows.remove(w)
            if w in self._compose_windows
            else None,
        )
        window.present()

    def _on_draft_save_started(
        self, account_uid: str, drafts_folder_name: str | None
    ) -> None:
        folder_name = drafts_folder_name
        if (
            folder_name is None
            and self._current_account is not None
            and self._current_account.uid == account_uid
            and self._current_folder is not None
            and self._sidebar.folder_is_drafts(account_uid, self._current_folder)
        ):
            folder_name = self._current_folder
        if folder_name is None:
            return
        self._suppress_local_draft_sync_reload(account_uid, folder_name)

    def _suppress_local_draft_sync_reload(
        self, account_uid: str, folder_name: str, *, seconds: float = 2.0
    ) -> None:
        key = (account_uid, folder_name)
        until = time.time() + seconds
        current = self._local_draft_sync_suppress_until.get(key, 0)
        if until > current:
            self._local_draft_sync_suppress_until[key] = until

    def _sync_list_reload_suppressed(
        self, account_uid: str, folder_name: str
    ) -> bool:
        key = (account_uid, folder_name)
        if time.time() < self._local_draft_sync_suppress_until.get(key, 0):
            return True
        self._local_draft_sync_suppress_until.pop(key, None)
        # Keep suppress for the whole in-flight move/archive — not one-shot.
        # Graph fires multiple folder-changed events while Camel is still in
        # transfer_messages_to_sync; clearing early queues a reload storm
        # behind the hung mail I/O thread (#189).
        if self._suppress_sync_list_reload == key:
            return True
        return False

    def _on_draft_saved(self, notification: SavedDraftNotification) -> None:
        self._suppress_local_draft_sync_reload(
            notification.account_uid, notification.folder_name
        )
        self._sidebar.refresh_folder_counts(
            notification.account_uid, notification.folder_name
        )
        if (
            self._current_account is None
            or self._current_account.uid != notification.account_uid
            or self._current_folder != notification.folder_name
        ):
            return

        if notification.removed:
            removed_uid = notification.previous_uid or notification.uid
            if removed_uid:
                self._message_list_view.remove_uids([removed_uid])
                self._remove_message_from_folder_cache(removed_uid)
                if self._current_message_uid == removed_uid:
                    self._clear_reader()
            return

        if notification.uid is None:
            return

        existing: dict | None = None
        for lookup_uid in (notification.uid, notification.previous_uid):
            if lookup_uid:
                existing = self._message_list_view.get_message(lookup_uid)
                if existing is not None:
                    break

        flags = dict((existing or {}).get("flags") or {})
        flags["attachments"] = notification.has_attachments

        message = {
            "uid": notification.uid,
            "subject": notification.subject or "(no subject)",
            "from": notification.from_label,
            "to": notification.to,
            "sort_date": notification.sort_date,
            "flags": flags,
        }
        self._message_list_view.upsert_message(
            message,
            folder_name=notification.folder_name,
            replace_uid=notification.previous_uid,
        )
        self._upsert_message_in_folder_cache(message, notification.previous_uid)
        if (
            notification.previous_uid
            and self._current_message_uid == notification.previous_uid
            and notification.uid is not None
        ):
            self._current_message_uid = notification.uid

    def _upsert_message_in_folder_cache(
        self,
        message: dict,
        previous_uid: str | None,
    ) -> None:
        if self._current_folder_messages is None:
            return
        messages = list(self._current_folder_messages)
        uid = str(message.get("uid") or "")
        if previous_uid and previous_uid != uid:
            messages = [item for item in messages if item.get("uid") != previous_uid]
        for index, item in enumerate(messages):
            if item.get("uid") == uid:
                messages[index] = dict(message)
                self._current_folder_messages = messages
                return
        messages.insert(0, dict(message))
        self._current_folder_messages = messages

    def _remove_message_from_folder_cache(self, uid: str) -> None:
        if self._current_folder_messages is None:
            return
        self._current_folder_messages = [
            message
            for message in self._current_folder_messages
            if message.get("uid") != uid
        ]

    def _remap_list_key(self, list_key: str | None, remaps: dict[str, str]) -> str | None:
        if list_key is None:
            return None
        if list_key in remaps:
            return remaps[list_key]
        parsed = parse_search_row_key(list_key)
        if parsed is not None:
            account_uid, folder_name, message_uid = parsed
            if message_uid in remaps:
                return make_search_row_key(
                    account_uid, folder_name, remaps[message_uid]
                )
        return list_key

    def _remap_folder_message_uids(self, remaps: dict[str, str]) -> None:
        """Rewrite list keys when RestId A→B remaps (#267 / #294).

        Search rows are keyed by ``account\\0folder\\0uid``, so a raw RestId
        lookup would miss them.
        """
        if not remaps:
            return

        def _lookup_existing(old_uid: str) -> tuple[str | None, dict | None]:
            existing = self._message_list_view.get_message(old_uid)
            if existing is not None:
                return old_uid, existing
            for message in self._current_folder_messages or []:
                if str(message.get("uid") or "") != old_uid:
                    continue
                list_key = self._message_list_key(message)
                found = self._message_list_view.get_message(list_key)
                return list_key, found if found is not None else dict(message)
            return None, None

        for old_uid, new_uid in remaps.items():
            if not old_uid or not new_uid or old_uid == new_uid:
                continue
            list_key, existing = _lookup_existing(old_uid)
            if existing is None:
                continue
            updated = dict(existing)
            updated["uid"] = new_uid
            account_uid = updated.get("_search_account_uid")
            folder_name = updated.get("_search_folder")
            if account_uid and folder_name:
                updated = annotate_search_match(
                    updated,
                    account_uid=str(account_uid),
                    folder_name=str(folder_name),
                )
            self._message_list_view.upsert_message(
                updated,
                folder_name=str(folder_name or self._current_folder or ""),
                replace_uid=list_key or old_uid,
            )
            self._upsert_message_in_folder_cache(updated, old_uid)

        mapped_current = self._remap_list_key(self._current_message_uid, remaps)
        if (
            mapped_current
            and mapped_current != self._current_message_uid
        ):
            self._current_message_uid = mapped_current
            set_active_message_uid(mapped_current)

        self._pending_restore_message_uid = self._remap_list_key(
            self._pending_restore_message_uid, remaps
        )
        if self._context_message_uids:
            self._context_message_uids = [
                self._remap_list_key(uid, remaps) or uid
                for uid in self._context_message_uids
            ]

    def _setup_undo_action(self) -> None:
        self._undo_move_action = Gio.SimpleAction.new("undo-move", None)
        self._undo_move_action.set_enabled(False)
        self._undo_move_action.connect("activate", self._on_undo_move_action)
        self.add_action(self._undo_move_action)

        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.undo-move", ["<Control>z"])

    def _on_undo_move_action(self, *_args) -> None:
        self._trigger_move_undo()

    def _setup_attachment_menu(self) -> None:
        save_action = Gio.SimpleAction.new("attachment-save", None)
        save_action.connect("activate", self._on_attachment_menu_save)
        self.add_action(save_action)

        open_with_action = Gio.SimpleAction.new("attachment-open-with", None)
        open_with_action.connect("activate", self._on_attachment_menu_open_with)
        self.add_action(open_with_action)

        add_cal_action = Gio.SimpleAction.new("attachment-add-to-calendar", None)
        add_cal_action.connect("activate", self._on_attachment_menu_add_to_calendar)
        self.add_action(add_cal_action)

        self._attachment_menu_model = Gio.Menu()
        self._attachment_menu_model.append("Save...", "win.attachment-save")
        self._attachment_menu_model.append("Open With…", "win.attachment-open-with")
        self._attachment_popover = Gtk.PopoverMenu.new_from_model(
            self._attachment_menu_model
        )

    def _setup_message_menu(self) -> None:
        mark_read_action = Gio.SimpleAction.new("message-mark-read", None)
        mark_read_action.connect("activate", self._on_message_menu_mark_read)
        self.add_action(mark_read_action)

        mark_unread_action = Gio.SimpleAction.new("message-mark-unread", None)
        mark_unread_action.connect("activate", self._on_message_menu_mark_unread)
        self.add_action(mark_unread_action)

        flag_action = Gio.SimpleAction.new("message-flag", None)
        flag_action.connect("activate", self._on_message_menu_flag)
        self.add_action(flag_action)

        unflag_action = Gio.SimpleAction.new("message-unflag", None)
        unflag_action.connect("activate", self._on_message_menu_unflag)
        self.add_action(unflag_action)

        self._archive_action = Gio.SimpleAction.new("message-archive", None)
        self._archive_action.connect("activate", self._on_message_menu_archive)
        self.add_action(self._archive_action)

        self._trash_action = Gio.SimpleAction.new("message-move-trash", None)
        self._trash_action.connect("activate", self._on_message_menu_move_trash)
        self.add_action(self._trash_action)

        reply_action = Gio.SimpleAction.new("message-reply", None)
        reply_action.connect("activate", self._on_message_menu_reply)
        self.add_action(reply_action)

        reply_all_action = Gio.SimpleAction.new("message-reply-all", None)
        reply_all_action.connect("activate", self._on_message_menu_reply_all)
        self.add_action(reply_all_action)

        forward_action = Gio.SimpleAction.new("message-forward", None)
        forward_action.connect("activate", self._on_message_menu_forward)
        self.add_action(forward_action)

        send_again_action = Gio.SimpleAction.new("message-send-again", None)
        send_again_action.connect("activate", self._on_message_menu_send_again)
        self.add_action(send_again_action)

        unsubscribe_action = Gio.SimpleAction.new("message-unsubscribe", None)
        unsubscribe_action.connect("activate", self._on_message_menu_unsubscribe)
        self.add_action(unsubscribe_action)

        add_calendar_action = Gio.SimpleAction.new("message-add-to-calendar", None)
        add_calendar_action.connect("activate", self._on_message_menu_add_to_calendar)
        self.add_action(add_calendar_action)

        self._outbox_edit_action = Gio.SimpleAction.new("message-outbox-edit", None)
        self._outbox_edit_action.connect("activate", self._on_message_menu_outbox_edit)
        self.add_action(self._outbox_edit_action)

        self._outbox_drafts_action = Gio.SimpleAction.new("message-outbox-drafts", None)
        self._outbox_drafts_action.connect(
            "activate", self._on_message_menu_outbox_move_drafts
        )
        self.add_action(self._outbox_drafts_action)

        self._outbox_send_now_action = Gio.SimpleAction.new(
            "message-outbox-send-now", None
        )
        self._outbox_send_now_action.connect(
            "activate", self._on_message_menu_outbox_send_now
        )
        self.add_action(self._outbox_send_now_action)

        self._message_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self._message_popover.set_parent(self._message_scroll)

    def _setup_message_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        for accelerator in ("Menu", "<Shift>F10"):
            trigger = Gtk.ShortcutTrigger.parse_string(accelerator)
            action = Gtk.CallbackAction.new(self._on_message_context_shortcut)
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))
        self._message_list_view.list_view.add_controller(controller)

    def _on_message_context_shortcut(
        self,
        _widget: Gtk.Widget,
        _args: GLib.Variant | None = None,
    ) -> bool:
        uid = self._message_list_view.get_primary_selected_uid()
        if uid is None:
            uids = self._message_list_view.get_selected_uids()
            uid = uids[0] if uids else None
        if uid is None:
            return False
        self._popup_message_menu(uid, 8, 8)
        return True

    def _ensure_popover_parent(
        self, popover: Gtk.PopoverMenu, widget: Gtk.Widget
    ) -> None:
        current = popover.get_parent()
        if current is widget:
            return
        if current is not None:
            popover.popdown()
            if popover.get_parent() is current:
                popover.unparent()
        popover.set_parent(widget)

    def begin_load(self) -> None:
        """Load accounts and folders after the window is on screen."""
        self._set_status(format_startup_loading_accounts())
        GLib.idle_add(self._begin_startup_load)

    def _begin_startup_load(self) -> bool:
        self._sync_watcher.set_current_folder(None, None)
        if not self._try_eager_restore_active_folder():
            self._clear_reader()
            self._message_popover.popdown()
            self._message_list_view.clear()
            self._current_account = None
            self._current_folder = None
            self._search_query = None
            self._update_search_entry_state()
        self._sidebar.load()
        return False

    def _try_eager_restore_active_folder(self) -> bool:
        sidebar_state = get_sidebar_state()
        active_folder = sidebar_state.get("active_folder")
        if active_folder is None:
            return False
        account_uid, folder_name = active_folder
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return False
        self._prepare_folder_selection(account, folder_name, sidebar_state)
        self._sidebar.mark_folder_active(account_uid, folder_name)
        self._load_messages(account_uid, folder_name)
        return True

    def _prepare_folder_selection(
        self,
        account: MailAccount,
        folder_name: str,
        sidebar_state: dict | None = None,
    ) -> None:
        if sidebar_state is None:
            sidebar_state = get_sidebar_state()
        self._pre_search_snapshot = None
        self._pre_search_folder = None
        self._current_account = account
        self._current_folder = folder_name
        self._message_list_view.set_drag_context(account.uid, folder_name)
        if self._sync_watcher.running:
            self._sync_watcher_current_folder()
        self._update_search_entry_state()
        selection = (account.uid, folder_name)
        saved_folder = sidebar_state.get("active_folder")
        saved_message = sidebar_state.get("active_message_uid")
        if saved_folder == selection and saved_message:
            self._restore_message_folder = saved_folder
            self._pending_restore_message_uid = saved_message
        else:
            self._restore_message_folder = selection
            self._pending_restore_message_uid = None
            if saved_folder != selection:
                set_active_message_uid(None)

    def _set_status(self, text: str) -> None:
        self._status_hint = text
        self._refresh_status_display()

    def _refresh_status_display(self) -> None:
        if not self._network_available:
            send_queued = len(list_queued_outbound_messages())
            operation_queued = self._mail.count_queued_operations()
            draft_queued = self._mail.count_queued_drafts()
            parts = [
                offline_queue_status_text(
                    send_queued_count=send_queued,
                    operation_queued_count=operation_queued,
                    draft_queued_count=draft_queued,
                )
            ]
            if self._search_query is not None:
                parts.append(OFFLINE_SEARCHING_LOCAL_CACHE)
            self._status.set_label(" · ".join(parts))
            return
        # Interactive folder load / search beats delay countdown and offline backfill.
        if self._status_hint and (
            self._message_sync_in_progress
            or self._offline_held_for_load_generation is not None
            or self._message_stack.get_visible_child_name() == "loading"
            or self._search_query is not None
        ):
            self._status.set_label(self._status_hint)
            return
        if self._send_delay_status:
            self._status.set_label(self._send_delay_status)
            return
        if self._offline_download_status:
            self._status.set_label(self._offline_download_status)
            return
        self._status.set_label(self._status_hint)

    def _interaction_parent_window(self) -> Gtk.Window:
        """Prefer a visible compose window for modal prompts over the main window."""
        for window in reversed(self._compose_windows):
            if window.get_visible() and window.is_active():
                return window
        for window in reversed(self._compose_windows):
            if window.get_visible():
                return window
        return self

    def _prompt_account_password(
        self,
        account_label: str,
        _mechanism: str | None,
        reason: str | None = None,
        service_uid: str | None = None,
    ) -> str | None:
        from post.mail.auth import PasswordPromptReason

        prompt_reason: PasswordPromptReason | None = None
        if reason in ("check_mail", "send_mail"):
            prompt_reason = reason  # type: ignore[assignment]
        password = prompt_password_sync(
            self._interaction_parent_window(),
            account_label,
            reason=prompt_reason,
        )
        if password is None:
            # Cancel / empty sign-in: mark the account immediately so the offline
            # badge appears even when Camel continues with a local cache.
            self._mark_service_needs_sign_in(service_uid, account_label)
        return password

    def _mark_service_needs_sign_in(
        self, service_uid: str | None, account_label: str
    ) -> None:
        account_uid = None
        if service_uid:
            account_uid = self._mail.resolve_account_uid_for_service(service_uid)
        if account_uid:
            self._mail.set_account_connect_health(account_uid, "needs_sign_in")
            return
        self._mark_accounts_need_sign_in(account_label)

    def _mark_accounts_need_sign_in(self, account_label: str) -> None:
        label = (account_label or "").strip().casefold()
        if not label:
            return
        try:
            accounts = self._mail.list_accounts()
        except Exception:
            log.exception("Could not list accounts after password cancel")
            return
        for account in accounts:
            candidates = {
                (account.display_label or "").strip().casefold(),
                (account.name or "").strip().casefold(),
                (account.email or "").strip().casefold(),
                (account.from_address or "").strip().casefold(),
            }
            if label in candidates:
                self._mail.set_account_connect_health(account.uid, "needs_sign_in")

    def _on_account_health_changed(self, account_uid: str) -> bool:
        sidebar = getattr(self, "_sidebar", None)
        if sidebar is not None:
            sidebar.refresh_account_online_marker(account_uid)
        return False

    def _reload_sidebar(self) -> bool:
        self._sync_watcher.set_current_folder(None, None)
        self._clear_reader()
        self._message_popover.popdown()
        self._message_list_view.clear()
        self._current_account = None
        self._current_folder = None
        self._search_query = None
        self._update_search_entry_state()
        self._sidebar.load()
        return False

    def _search_empty_label(self, folder_label: str) -> str:
        if self._search_scope.kind == SEARCH_SCOPE_ALL:
            return "No Matches in All Mail"
        if self._search_scope.kind == SEARCH_SCOPE_ACCOUNT:
            account_label = self._sidebar.account_display_label(
                self._search_scope.account_uid or ""
            )
            return f"No Matches in {account_label}"
        return f"No Matches in {folder_label}"

    def _search_target_label(self, folder_label: str) -> str:
        if self._search_scope.kind == SEARCH_SCOPE_ALL:
            return "All Mail"
        if self._search_scope.kind == SEARCH_SCOPE_ACCOUNT:
            return self._sidebar.account_display_label(
                self._search_scope.account_uid or ""
            )
        account_label, folder_display = self._search_folder_scope_labels(
            folder_label
        )
        target = format_search_target_label(
            account_label=account_label,
            folder_label=folder_display,
        )
        return target or folder_label

    def _search_folder_scope_labels(
        self,
        folder_display: str,
        *,
        account_uid: str | None = None,
    ) -> tuple[str | None, str | None]:
        account_label: str | None = None
        if self._current_account and (
            account_uid is None or self._current_account.uid == account_uid
        ):
            account_label = self._current_account.display_label
        elif account_uid:
            account_label = self._sidebar.account_display_label(account_uid)
        elif self._search_scope.account_uid:
            account_label = self._sidebar.account_display_label(
                self._search_scope.account_uid
            )
        return account_label, folder_display

    def _is_multi_folder_search_active(self) -> bool:
        return (
            self._search_scope.kind != SEARCH_SCOPE_FOLDER
            and self._search_query is not None
        )

    def _is_multi_folder_scope(self) -> bool:
        return self._search_scope.kind != SEARCH_SCOPE_FOLDER

    def _message_list_key(self, message: dict) -> str:
        row_key = message.get("_search_row_key")
        if row_key:
            return str(row_key)
        return str(message.get("uid") or "")

    def _reader_shows_list_key(self, list_key: str) -> bool:
        """True when the reader pane is displaying the message for ``list_key``."""
        if self._current_message is None:
            return False
        if self._message_list_key(self._current_message) == list_key:
            return True
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return False
        expected_account, expected_folder, expected_uid = location
        message_uid = str(self._current_message.get("uid") or "")
        if message_uid != expected_uid:
            return False
        search_account = self._current_message.get("_search_account_uid")
        search_folder = self._current_message.get("_search_folder")
        if search_account and search_folder:
            return (
                str(search_account) == expected_account
                and str(search_folder) == expected_folder
            )
        if self._current_account and self._current_folder:
            return (
                self._current_account.uid == expected_account
                and self._current_folder == expected_folder
            )
        return True

    def _message_location_for_list_key(
        self, list_key: str
    ) -> tuple[str, str, str] | None:
        parsed = parse_search_row_key(list_key)
        if parsed is not None:
            return parsed
        if self._current_folder_messages:
            for message in self._current_folder_messages:
                if self._message_list_key(message) == list_key:
                    account_uid = message.get("_search_account_uid")
                    folder_name = message.get("_search_folder")
                    message_uid = str(message.get("uid") or "")
                    if account_uid and folder_name and message_uid:
                        return str(account_uid), str(folder_name), message_uid
                    if self._current_account and self._current_folder:
                        return (
                            self._current_account.uid,
                            self._current_folder,
                            message_uid,
                        )
        if (
            self._current_account
            and self._current_folder
            and "\0" not in list_key
        ):
            return self._current_account.uid, self._current_folder, list_key
        return None

    def _search_result_meta_label(self, message: dict) -> str | None:
        account_uid = message.get("_search_account_uid")
        folder_name = message.get("_search_folder")
        if not account_uid or not folder_name:
            return None
        account_label = self._sidebar.account_display_label(str(account_uid))
        folder_display = self._sidebar.folder_display_name(
            str(account_uid), str(folder_name)
        )
        if is_post_outbox_folder(str(folder_name)):
            sender = message.get("preview_to") or message.get("to") or ""
        else:
            sender = message.get("from") or ""
        return format_search_result_meta(account_label, folder_display, sender)

    def _update_search_scope_ui(self) -> None:
        if self._is_multi_folder_search_active():
            self._message_list_view.set_search_meta_label_resolver(
                self._search_result_meta_label
            )
            self._message_list_view.set_drag_context(None, None)
        else:
            self._message_list_view.set_search_meta_label_resolver(None)
            if self._current_account and self._current_folder:
                self._message_list_view.set_drag_context(
                    self._current_account.uid, self._current_folder
                )

    def _selected_message_source_matches_sidebar(self) -> bool:
        if not self._is_multi_folder_search_active():
            return True
        selected = self._message_list_view.get_selected_uids()
        if len(selected) != 1:
            return False
        location = self._message_location_for_list_key(selected[0])
        if location is None:
            return False
        account_uid, folder_name, _message_uid = location
        return (
            self._current_account is not None
            and self._current_folder is not None
            and account_uid == self._current_account.uid
            and folder_name == self._current_folder
        )

    def _enter_multi_folder_sidebar_mode(self) -> None:
        if self._current_account and self._current_folder:
            self._folder_before_multi_folder_search = (
                self._current_account.uid,
                self._current_folder,
            )
        self._sidebar.clear_folder_selection()

    def _leave_multi_folder_sidebar_mode(self) -> None:
        saved = self._folder_before_multi_folder_search
        self._folder_before_multi_folder_search = None
        if saved is None:
            return
        account_uid, folder_name = saved
        if not self._sidebar.restore_folder_selection(account_uid, folder_name):
            self._sidebar.mark_folder_active(account_uid, folder_name)

    def _sync_multi_folder_sidebar_selection(self) -> None:
        if self._is_multi_folder_scope():
            self._enter_multi_folder_sidebar_mode()
        else:
            self._leave_multi_folder_sidebar_mode()

    def _set_search_scope_dropdown_selected(self, scope: SearchScope) -> None:
        for index, item in enumerate(self._search_scope_items):
            if item == scope:
                self._search_scope_dropdown_updating = True
                try:
                    self._search_scope_dropdown.set_selected(index)
                finally:
                    self._search_scope_dropdown_updating = False
                return
        folder_scope = SearchScope(SEARCH_SCOPE_FOLDER)
        for index, item in enumerate(self._search_scope_items):
            if item == folder_scope:
                self._search_scope_dropdown_updating = True
                try:
                    self._search_scope_dropdown.set_selected(index)
                finally:
                    self._search_scope_dropdown_updating = False
                return

    def _rebuild_search_scope_dropdown(self, account_uids: list[str]) -> None:
        items: list[SearchScope] = [SearchScope(SEARCH_SCOPE_FOLDER)]
        labels = ["Selected Folder"]
        # Per-account scopes only matter with 2+ accounts; a single account
        # entry would duplicate "All Mail" (#222).
        if len(account_uids) > 1:
            for account_uid in account_uids:
                items.append(
                    SearchScope(SEARCH_SCOPE_ACCOUNT, account_uid=account_uid)
                )
                labels.append(self._sidebar.account_display_label(account_uid))
        items.append(SearchScope(SEARCH_SCOPE_ALL))
        labels.append("All Mail")

        scope = self._search_scope
        if scope.kind == SEARCH_SCOPE_ACCOUNT:
            if scope.account_uid not in account_uids:
                scope = SearchScope(SEARCH_SCOPE_FOLDER)
                self._search_scope = scope
                set_search_scope(scope)
            elif len(account_uids) <= 1:
                # Hidden account entry is equivalent to All Mail.
                scope = SearchScope(SEARCH_SCOPE_ALL)
                self._search_scope = scope
                set_search_scope(scope)

        self._search_scope_items = items
        model = Gtk.StringList.new(labels)
        self._search_scope_dropdown_updating = True
        try:
            self._search_scope_dropdown.set_model(model)
            self._set_search_scope_dropdown_selected(scope)
        finally:
            self._search_scope_dropdown_updating = False

    def _on_search_scope_changed(self, _dropdown: Gtk.DropDown, *_args) -> None:
        if self._search_scope_dropdown_updating:
            return
        selected = _dropdown.get_selected()
        if selected < 0 or selected >= len(self._search_scope_items):
            return
        scope = self._search_scope_items[selected]
        self._search_scope = scope
        set_search_scope(scope)
        self._sync_multi_folder_sidebar_selection()
        if self._is_multi_folder_scope() and self._parse_search_from_entry() is not None:
            self._apply_search_from_entry()
        elif not self._is_multi_folder_scope():
            self._update_search_scope_ui()

    def _update_search_entry_state(self) -> None:
        folder_selected = (
            self._current_account is not None
            and self._current_folder is not None
            and not is_post_outbox_folder(self._current_folder)
        )
        # Keep the bar disabled until the folder tree has finished loading so
        # search cannot preempt sidebar assembly (#144 / #196).
        enabled = folder_selected and self._sidebar.folder_tree_ready
        self._header_search_entry.set_sensitive(enabled)
        self._search_scope_dropdown.set_sensitive(enabled)
        if not enabled:
            self._search_entry_updating = True
            self._header_search_entry.set_text("")
            self._search_entry_updating = False
            self._search_query = None
            if not folder_selected and self._is_multi_folder_scope():
                folder_scope = SearchScope(SEARCH_SCOPE_FOLDER)
                self._search_scope = folder_scope
                set_search_scope(folder_scope)
                self._set_search_scope_dropdown_selected(folder_scope)
                self._leave_multi_folder_sidebar_mode()

    def _parse_search_from_entry(self) -> MessageSearchQuery | None:
        raw = self._header_search_entry.get_text()
        if not raw.strip():
            return None
        return parse_search_query(raw)

    def _on_search_activate(self, _entry: Gtk.SearchEntry) -> None:
        """Run search immediately when Enter is pressed."""
        self._apply_search_from_entry()

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_entry_updating:
            return
        self._apply_search_from_entry()

    def _preserve_pre_search_snapshot(self) -> None:
        if not self._current_account or not self._current_folder:
            return
        messages = self._current_folder_messages
        if not messages:
            return
        unread = sum(
            1
            for message in messages
            if not (message.get("flags") or {}).get("seen", False)
        )
        self._pre_search_snapshot = (
            list(messages),
            unread,
            self._message_total,
            self._message_list_source,
        )
        self._pre_search_folder = (self._current_account.uid, self._current_folder)

    def _take_pre_search_snapshot(
        self, account_uid: str, folder_name: str
    ) -> tuple[list[dict], int, int, str] | None:
        if (
            self._pre_search_snapshot is None
            or self._pre_search_folder != (account_uid, folder_name)
        ):
            return None
        snapshot = self._pre_search_snapshot
        self._pre_search_snapshot = None
        self._pre_search_folder = None
        return snapshot

    def _restore_messages_after_search(self) -> None:
        if not self._current_account or not self._current_folder:
            return
        self._mail.cancel_folder_search()
        account_uid = self._current_account.uid
        folder_name = self._current_folder
        snapshot = self._take_pre_search_snapshot(account_uid, folder_name)
        if snapshot is not None:
            messages, unread, total, source = snapshot
            self._messages_load_generation += 1
            load_id = self._messages_load_generation
            self._messages_load_expects_search = False
            self._message_popover.popdown()
            self._clear_reader()
            self._on_messages_loaded(
                load_id,
                account_uid,
                folder_name,
                messages,
                unread,
                total,
                source,
                False,
                None,
            )
            return
        self._load_messages(
            account_uid,
            folder_name,
            offset=0,
            sync=False,
        )

    def _narrow_search_to_folder(
        self,
        account: MailAccount,
        folder_name: str,
        query: MessageSearchQuery,
        *,
        seed_matches: list[dict],
    ) -> None:
        folder_scope = SearchScope(SEARCH_SCOPE_FOLDER)
        self._search_scope = folder_scope
        set_search_scope(folder_scope)
        self._set_search_scope_dropdown_selected(folder_scope)
        self._folder_before_multi_folder_search = None
        self._search_query = query
        self._update_search_scope_ui()
        search_trace(
            "search_narrow_to_folder",
            account=account.uid,
            folder=folder_name,
            terms=len(query.terms),
            seed_count=len(seed_matches),
        )
        self._load_messages(
            account.uid,
            folder_name,
            offset=0,
            sync=False,
            seed_search_matches=seed_matches or None,
        )

    def _apply_search_from_entry(self) -> None:
        if self._search_entry_updating:
            return
        if not self._current_account or not self._current_folder:
            return
        if not self._sidebar.folder_tree_ready:
            return

        raw = self._header_search_entry.get_text()
        query = parse_search_query(raw)
        if query is None:
            self._search_query = None
            self._restore_messages_after_search()
            return

        self._preserve_pre_search_snapshot()
        self._search_query = query
        self._update_search_scope_ui()
        if self._is_multi_folder_scope():
            self._enter_multi_folder_sidebar_mode()
        search_trace(
            "search_apply",
            raw=raw,
            terms=len(query.terms),
            account=self._current_account.uid,
            folder=self._current_folder,
        )
        self._load_messages(
            self._current_account.uid,
            self._current_folder,
            offset=0,
            sync=False,
        )

    def _on_search_stopped(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_entry_updating:
            return
        self._exit_search_mode()

    def _exit_search_mode(self) -> None:
        self._search_query = None
        self._update_search_scope_ui()
        if self._is_multi_folder_scope():
            self._sync_multi_folder_sidebar_selection()
        self._search_entry_updating = True
        self._header_search_entry.set_text("")
        self._search_entry_updating = False
        self._restore_messages_after_search()

    def _on_folder_selected(self, account: MailAccount, folder_name: str) -> None:
        query = self._parse_search_from_entry()
        narrowing = self._is_multi_folder_scope() and query is not None
        already_current = (
            self._current_account is not None
            and self._current_account.uid == account.uid
            and self._current_folder == folder_name
        )
        seed_source = (
            list(self._current_folder_messages or []) if narrowing else []
        )
        self._prepare_folder_selection(account, folder_name)
        if narrowing and query is not None:
            seed_matches = filter_search_matches_for_folder(
                seed_source,
                account_uid=account.uid,
                folder_name=folder_name,
            )
            self._narrow_search_to_folder(
                account,
                folder_name,
                query,
                seed_matches=seed_matches,
            )
            return
        # Eager restore / re-select of the active row only needs search state
        # refreshed — avoid kicking off a duplicate message load (#196).
        if already_current:
            return
        self._search_query = query
        self._load_messages(account.uid, folder_name)

    def _show_message_unavailable_reader(self, message: str) -> None:
        self._current_message = None
        self._reader_pane.show_unavailable(message, dark=self._app_prefers_dark())

    def _remove_vanished_message(self, uid: str) -> None:
        if not self._current_account or not self._current_folder:
            return

        folder_name = self._current_folder
        removed = self._message_list_view.remove_uids([uid])
        if uid == self._current_message_uid:
            self._current_message_uid = None
            set_active_message_uid(None)
            self._restore_message_folder = None

        if self._current_folder_messages:
            self._current_folder_messages = [
                message
                for message in self._current_folder_messages
                if self._message_list_key(message) != uid
            ]

        if removed and self._message_total >= 0:
            self._message_total = max(0, self._message_total - 1)

        if self._message_list_view.item_count() == 0 and folder_name:
            self._message_empty_label.set_label(f"No Messages in {folder_name}")
            self._message_stack.set_visible_child_name("empty")

        self._mail.invalidate_folder_index(
            self._current_account.uid, folder_name
        )
        self._update_message_status(self._current_account, folder_name)

    def _clear_reader(self) -> None:
        self._message_read_generation += 1
        self._pending_message_read_uid = None
        self._inflight_message_read_id = None
        self._current_message_uid = None
        self._current_message = None
        self._reader_pane.clear()
        self._update_message_toolbar()
        GLib.idle_add(self._reload_reader_for_selection_if_needed)

    def _reload_reader_for_selection_if_needed(self) -> bool:
        if self._message_stack.get_visible_child_name() != "list":
            return False
        if not self._current_account or not self._current_folder:
            return False
        if self._current_message is not None:
            return False
        selected = self._message_list_view.get_selected_uids()
        if len(selected) != 1:
            return False
        uid = selected[0]
        self._current_message_uid = uid
        self._load_message_body_for_uid(
            uid,
            mark_seen=self._mark_seen_when_reading_uid(uid),
        )
        return False

    def _on_reader_attachment_context_menu(
        self,
        widget: Gtk.Widget,
        x: float,
        y: float,
        index: int,
        mime_type: str | None,
        name: str,
    ) -> None:
        from post.mail.calendar_invite import looks_like_calendar_attachment

        self._context_attachment_index = index
        self._context_attachment_mime = mime_type
        self._context_attachment_name = name
        menu = Gio.Menu()
        menu.append("Save...", "win.attachment-save")
        menu.append("Open With…", "win.attachment-open-with")
        if looks_like_calendar_attachment(mime_type, name):
            menu.append("Add to Calendar…", "win.attachment-add-to-calendar")
        self._attachment_popover.set_menu_model(menu)
        self._ensure_popover_parent(self._attachment_popover, widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._attachment_popover.set_pointing_to(rect)
        self._attachment_popover.popup()

    def _on_reader_attachment_clicked(self, attachment_index: int) -> None:
        self._fetch_attachment(attachment_index, self._open_attachment_direct)

    def _on_attachment_menu_save(self, *_args) -> None:
        if self._context_attachment_index is None:
            return
        self._fetch_attachment(
            self._context_attachment_index,
            self._prompt_save_attachment,
        )

    def _on_attachment_menu_open_with(self, *_args) -> None:
        if self._context_attachment_index is None:
            return
        self._fetch_attachment(
            self._context_attachment_index,
            self._prompt_open_with_dialog,
        )

    def _on_attachment_menu_add_to_calendar(self, *_args) -> None:
        invite = None
        if self._current_message and isinstance(
            self._current_message.get("calendar_invite"), dict
        ):
            invite = dict(self._current_message["calendar_invite"])
        if invite is None:
            show_error_toast(self, "No calendar invite details found")
            return
        self._on_add_to_calendar_clicked(invite)

    def _fetch_attachment(
        self,
        attachment_index: int,
        on_ready: Callable[[str, bytes | None, Exception | None], None],
    ) -> None:
        list_key = self._current_message_uid
        if not list_key:
            return
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return
        account_uid, folder_name, message_uid = location

        def worker() -> None:
            error: Exception | None = None
            filename = "attachment"
            data: bytes | None = None
            try:
                filename, data = self._mail.read_attachment_data(
                    account_uid, folder_name, message_uid, attachment_index
                )
            except Exception as exc:
                log.exception("Failed to read attachment")
                error = exc
            GLib.idle_add(
                self._on_attachment_fetched,
                filename,
                data,
                error,
                on_ready,
            )

        get_mail_io_thread().submit(worker)

    def _on_attachment_fetched(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
        on_ready: Callable[[str, bytes | None, Exception | None], None],
    ) -> bool:
        on_ready(filename, data, error)
        return False

    def _open_attachment_direct(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Attachment error: {error}")
            return
        if data is None:
            show_error_toast(self, "Attachment error: no data")
            return

        try:
            path = write_temp_attachment(filename, data)
            file = Gio.File.new_for_path(path)
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except (OSError, GLib.Error) as exc:
            show_error_toast(self, f"Could not open attachment: {exc}")
            return

        self._set_status(f"Opened {os.path.basename(filename)}")

    def _prompt_save_attachment(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Attachment error: {error}")
            return
        if data is None:
            show_error_toast(self, "Attachment error: no data")
            return

        dialog = Gtk.FileDialog(title="Save Attachment")
        dialog.set_initial_name(filename)
        dialog.save(self, None, self._on_attachment_save_finished, (filename, data))

    def _on_attachment_save_finished(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        user_data: tuple[str, bytes],
    ) -> None:
        filename, data = user_data
        try:
            file = dialog.save_finish(result)
        except GLib.Error as exc:
            if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                return
            show_error_toast(self, f"Save error: {exc.message}")
            return

        path = file.get_path()
        if path is None:
            show_error_toast(self, "Save error: no path")
            return

        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            show_error_toast(self, f"Save error: {exc}")
            return

        self._set_status(f"Saved {os.path.basename(filename)}")

    def _prompt_open_with_dialog(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Attachment error: {error}")
            return
        if data is None:
            show_error_toast(self, "Attachment error: no data")
            return

        try:
            path = write_temp_attachment(filename, data)
        except OSError as exc:
            show_error_toast(self, f"Could not open attachment: {exc}")
            return

        content_type = self._guess_content_type(
            filename,
            data,
            self._context_attachment_mime,
        )
        dialog = Gtk.AppChooserDialog.new_for_content_type(
            self,
            Gtk.DialogFlags.MODAL,
            content_type,
        )
        dialog.set_heading("Open With")
        dialog.connect("response", self._on_app_chooser_response, (path, filename))
        dialog.present()

    def _on_app_chooser_response(
        self,
        dialog: Gtk.AppChooserDialog,
        response: int,
        user_data: tuple[str, str],
    ) -> None:
        path, filename = user_data
        if response == Gtk.ResponseType.OK:
            app_info = dialog.get_app_info()
            if app_info is not None:
                file = Gio.File.new_for_path(path)
                try:
                    app_info.launch_uris([file.get_uri()], None)
                    self._set_status(f"Opened {os.path.basename(filename)}")
                except GLib.Error as exc:
                    show_error_toast(self, f"Could not open attachment: {exc.message}")
        dialog.destroy()

    @staticmethod
    def _guess_content_type(filename: str, data: bytes, mime_hint: str | None = None) -> str:
        if mime_hint:
            return mime_hint
        guessed, _certain = Gio.content_type_guess(filename, data)
        return guessed or "application/octet-stream"

    def _on_refresh(self, *_args) -> None:
        if self._current_account and self._current_folder:
            self._search_query = self._parse_search_from_entry()
            self._load_messages(
                self._current_account.uid, self._current_folder, sync=True
            )
        else:
            GLib.idle_add(self._reload_sidebar)

    def _apply_folder_messages(
        self,
        messages: list[dict],
        folder_name: str,
        *,
        account: MailAccount | None = None,
    ) -> None:
        self._message_list_view.set_messages(messages, folder_name=folder_name)
        if account is not None:
            self._update_message_status(account, folder_name)

    def _apply_prepended_folder_messages(
        self,
        prepended: list[dict],
        folder_name: str,
        *,
        account: MailAccount | None = None,
    ) -> None:
        self._message_list_view.prepend_messages(prepended, folder_name=folder_name)
        if account is not None:
            self._update_message_status(account, folder_name)

    def _apply_messages_to_list(
        self,
        messages: list[dict],
        folder_name: str,
        *,
        account: MailAccount,
        load_id: int,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        # Keep full ``messages`` in ``_current_folder_messages``; only bind an
        # initial prefix into Gtk.ListView so huge folders do not freeze the
        # main thread. More rows append when the user scrolls near the end (#208).
        bind_messages = messages[:MESSAGE_LIST_UI_BIND_CAP]
        self._message_list_bound_count = len(bind_messages)
        batch_size = MESSAGE_LIST_UI_BATCH_SIZE
        if len(bind_messages) <= batch_size:
            self._apply_folder_messages(bind_messages, folder_name, account=account)
            self._message_list_view.reset_near_end()
            if on_complete is not None:
                on_complete()
            self._maybe_run_pending_sync_folder_refresh()
            return

        self._message_list_populating = True
        first_batch = bind_messages[:batch_size]
        self._apply_folder_messages(first_batch, folder_name, account=account)

        def append_batches(offset: int) -> bool:
            if load_id != self._messages_load_generation:
                self._message_list_populating = False
                return False
            next_offset = offset + batch_size
            batch = bind_messages[offset:next_offset]
            if batch:
                self._message_list_view.append_messages(batch, folder_name=folder_name)
            if next_offset < len(bind_messages):
                GLib.idle_add(append_batches, next_offset)
                return False
            self._message_list_populating = False
            self._message_list_view.reset_near_end()
            self._update_message_status(account, folder_name)
            if on_complete is not None:
                on_complete()
            self._maybe_run_pending_sync_folder_refresh()
            return False

        GLib.idle_add(append_batches, batch_size)

    def _on_message_list_near_end(self) -> None:
        """Append the next page of ``_current_folder_messages`` into the list."""
        if self._message_list_populating:
            self._message_list_view.reset_near_end()
            return
        if self._search_query is not None:
            return
        messages = self._current_folder_messages
        if not messages:
            return
        folder_name = self._current_folder
        account = self._current_account
        if not folder_name or account is None:
            return
        already = self._message_list_bound_count
        if already >= len(messages):
            return
        more = messages[already : already + MESSAGE_LIST_UI_BIND_MORE]
        if not more:
            return
        self._message_list_view.append_messages(more, folder_name=folder_name)
        self._message_list_bound_count = already + len(more)
        if is_heavy_folder_name(folder_name):
            store_n = self._message_list_view.item_count()
            _log_heavy_pipeline(
                "list",
                account.uid,
                folder_name,
                pipeline_id=self._heavy_pipeline_id or "ui-append",
                action="near_end_append",
                appended=len(more),
                bound=self._message_list_bound_count,
                indexed=len(messages),
                gtk_store=store_n,
                visible=(
                    "scroll_or_near_end"
                    if store_n > already
                    else "no_gtk_change"
                ),
            )
            log.debug(
                "Heavy-folder UI append bound %s/%s now=%d of %d gtk_store=%d",
                account.uid,
                folder_name,
                self._message_list_bound_count,
                len(messages),
                store_n,
            )
        self._update_message_status(account, folder_name)
        # Keep loading if the scrollbar is still pinned at the bottom (drag-to-end).
        self._message_list_view.after_content_appended()

    def _schedule_bind_unbound_heavy_messages(self, load_id: int) -> None:
        """Bind already-indexed older rows without rebuilding the top (#208).

        Graph backfill does not change the newest window, so a full rebind looks
        idle. Append unbound rows in batches so the list length grows and scroll
        reaches newly indexed mail even while refresh_info is still running.
        """
        if self._heavy_bind_catchup_load_id == load_id:
            return
        self._heavy_bind_catchup_load_id = load_id

        def _append_batch() -> bool:
            if load_id != self._messages_load_generation:
                if self._heavy_bind_catchup_load_id == load_id:
                    self._heavy_bind_catchup_load_id = None
                return False
            if self._search_query is not None:
                self._heavy_bind_catchup_load_id = None
                return False
            messages = self._current_folder_messages
            account = self._current_account
            folder_name = self._current_folder
            if not messages or account is None or not folder_name:
                self._heavy_bind_catchup_load_id = None
                return False
            already = self._message_list_bound_count
            if already >= len(messages):
                self._heavy_bind_catchup_load_id = None
                return False
            if self._message_list_populating:
                return True
            more = messages[already : already + MESSAGE_LIST_UI_BIND_MORE]
            if not more:
                self._heavy_bind_catchup_load_id = None
                return False
            self._message_list_view.append_messages(more, folder_name=folder_name)
            self._message_list_bound_count = already + len(more)
            store_n = self._message_list_view.item_count()
            pipeline_id = self._heavy_pipeline_id or "ui-catchup"
            _log_heavy_pipeline(
                "list",
                account.uid,
                folder_name,
                pipeline_id=pipeline_id,
                action="catchup_bind",
                load_id=load_id,
                appended=len(more),
                bound=self._message_list_bound_count,
                indexed=len(messages),
                gtk_store=store_n,
                visible=(
                    "older_rows_below_top_scroll_for_new"
                    if store_n >= self._message_list_bound_count
                    else "gtk_store_lag"
                ),
                note="top_unchanged_scroll_down_for_new_rows",
            )
            log.debug(
                "Heavy-folder UI catch-up bind %s/%s now=%d of %d gtk_store=%d",
                account.uid,
                folder_name,
                self._message_list_bound_count,
                len(messages),
                store_n,
            )
            self._update_message_status(account, folder_name)
            if self._message_list_bound_count < len(messages):
                return True
            self._heavy_bind_catchup_load_id = None
            return False

        GLib.timeout_add(50, _append_batch)

    def _message_flags_for_uid(self, uid: str) -> dict:
        message = self._message_list_view.get_message(uid)
        if message is not None:
            return dict(message.get("flags") or {})
        if self._current_message_uid == uid and self._current_message is not None:
            return dict(self._current_message.get("flags") or {})
        return {}

    @staticmethod
    def _message_seen_states_for_uids(
        uids: list[str], flags_for_uid: Callable[[str], dict]
    ) -> list[bool]:
        return [flags_for_uid(uid).get("seen", True) for uid in uids]

    @staticmethod
    def _message_flagged_states_for_uids(
        uids: list[str], flags_for_uid: Callable[[str], dict]
    ) -> list[bool]:
        return [flags_for_uid(uid).get("flagged", False) for uid in uids]

    @staticmethod
    def _count_menu_label(base: str, count: int) -> str:
        suffix = f" ({count})" if count > 1 else ""
        return f"{base}{suffix}"

    def _start_background_message_sync(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        fetch_messages: Callable[[bool], tuple[list[dict], int, int, str]],
    ) -> None:
        def worker_sync() -> None:
            error: Exception | None = None
            messages: list[dict] | None = None
            unread = -1
            total = -1
            try:
                messages, unread, total, _source = fetch_messages(True)
            except Exception as exc:
                if isinstance(exc, GLib.Error) and exc.matches(
                    Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
                ):
                    # Preempted by interactive folder load / search (#170 hang).
                    return
                if is_network_unavailable_error(exc):
                    GLib.idle_add(
                        self._on_messages_sync_finished,
                        load_id,
                        False,
                    )
                    return
                log_mail_error(log, "Failed to refresh messages", exc)
                error = exc
            GLib.idle_add(
                self._on_messages_refreshed,
                load_id,
                account_uid,
                folder_name,
                messages,
                unread,
                total,
                error,
            )

        # Background so folder switches can preempt long refresh_info_sync calls.
        get_mail_io_thread().submit_background(worker_sync)

    def _start_background_heavy_folder_index(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        *,
        cursor: dict | None = None,
    ) -> None:
        """Chunked Archive/Trash/Junk header index; preemptible (#208)."""
        self._heavy_index_in_progress = (account_uid, folder_name)
        log.debug(
            "Heavy-folder UI start index load_id=%s %s/%s cursor=%s",
            load_id,
            account_uid,
            folder_name,
            sorted((cursor or {}).keys()),
        )
        get_mail_io_thread().submit_background(
            self._run_heavy_folder_index_slice,
            load_id,
            account_uid,
            folder_name,
            cursor,
        )

    def _heavy_index_still_active(
        self, account_uid: str, folder_name: str
    ) -> bool:
        """True while Archive/etc. indexing should keep running for this folder."""
        return (
            self._heavy_index_in_progress == (account_uid, folder_name)
            and self._current_account is not None
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
        )

    def _adopt_heavy_index_load_id(
        self, load_id: int, account_uid: str, folder_name: str
    ) -> int | None:
        """Keep indexing across same-folder reloads that bump generation (#208).

        ``_load_messages`` always increments ``_messages_load_generation``. If we
        drop stale slices and also refuse to start a second indexer, Archive
        growth freezes until the user leaves and re-opens the folder.
        """
        if load_id == self._messages_load_generation:
            return load_id
        if self._heavy_index_still_active(account_uid, folder_name):
            return self._messages_load_generation
        if self._heavy_index_in_progress == (account_uid, folder_name):
            self._heavy_index_in_progress = None
        return None

    def _run_heavy_folder_index_slice(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        cursor: dict | None,
    ) -> None:
        """Continue heavy-folder indexing on the mail I/O thread (#208)."""
        load_id_opt = self._adopt_heavy_index_load_id(
            load_id, account_uid, folder_name
        )
        if load_id_opt is None:
            return
        load_id = load_id_opt
        if cursor and cursor.get("pending_server_refresh"):
            # Same wording as progress updates — avoid flickering with the
            # "...indexed so far" / "...of N on server" lines (#208 Spam).
            GLib.idle_add(
                self._set_status,
                f"Fetching {folder_name} headers…",
            )
        try:
            def _mid_refresh_progress(mid: object) -> None:
                GLib.idle_add(
                    self._on_heavy_folder_index_progress,
                    load_id,
                    account_uid,
                    folder_name,
                    mid,
                )

            progress = self._mail.continue_heavy_folder_index(
                account_uid,
                folder_name,
                cursor=cursor,
                on_progress=_mid_refresh_progress,
            )
        except Exception as exc:
            if isinstance(exc, GLib.Error) and exc.matches(
                Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED
            ):
                # Soft cancel / leave — resume if this folder is still open.
                if self._heavy_index_still_active(account_uid, folder_name):
                    resume_id = self._messages_load_generation
                    resume_cursor = {
                        "refresh_done": False,
                        "pending_server_refresh": True,
                        "status_seeded": True,
                        "did_prepare_content_refresh": True,
                    }

                    def _resume_after_cancel() -> bool:
                        if not self._heavy_index_still_active(
                            account_uid, folder_name
                        ):
                            return False
                        self._start_background_heavy_folder_index(
                            resume_id,
                            account_uid,
                            folder_name,
                            cursor=resume_cursor,
                        )
                        return False

                    GLib.timeout_add(500, _resume_after_cancel)
                    return
                if self._heavy_index_in_progress == (account_uid, folder_name):
                    self._heavy_index_in_progress = None
                GLib.idle_add(self._on_messages_sync_finished, load_id, False)
                return
            if is_network_unavailable_error(exc):
                if self._heavy_index_in_progress == (account_uid, folder_name):
                    self._heavy_index_in_progress = None
                GLib.idle_add(self._on_messages_sync_finished, load_id, False)
                return
            log_mail_error(log, "Failed to index heavy folder", exc)
            if self._heavy_index_in_progress == (account_uid, folder_name):
                self._heavy_index_in_progress = None
            GLib.idle_add(self._on_messages_sync_finished, load_id, False)
            return

        load_id_opt = self._adopt_heavy_index_load_id(
            load_id, account_uid, folder_name
        )
        if load_id_opt is None:
            return
        load_id = load_id_opt

        GLib.idle_add(
            self._on_heavy_folder_index_progress,
            load_id,
            account_uid,
            folder_name,
            progress,
        )
        if progress.done:
            return
        cursor = progress.cursor or {}
        if cursor.get("yield_for_interactive"):
            # Avoid a tight background resubmit loop while interactive mail I/O
            # is queued (#208).
            def _resume_after_yield() -> bool:
                adopted = self._adopt_heavy_index_load_id(
                    load_id, account_uid, folder_name
                )
                if adopted is None:
                    return False
                self._start_background_heavy_folder_index(
                    adopted,
                    account_uid,
                    folder_name,
                    cursor=cursor,
                )
                return False

            GLib.timeout_add(250, _resume_after_yield)
            return
        # Yield to interactive work, then resume from cursor.
        get_mail_io_thread().submit_background(
            self._run_heavy_folder_index_slice,
            load_id,
            account_uid,
            folder_name,
            progress.cursor,
        )

    def _on_heavy_folder_index_progress(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        progress,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False
        if self._search_query is not None:
            return False
        account = self._current_account
        if account is None or account.uid != account_uid:
            return False
        if self._current_folder != folder_name:
            return False

        remaps = getattr(progress, "uid_remaps", None) or {}
        if remaps:
            self._remap_folder_message_uids(remaps)

        messages = progress.messages
        indexed = len(messages)
        cursor = progress.cursor or {}
        pipeline_id = str(cursor.get("pipeline_id") or "") or None
        if pipeline_id:
            self._heavy_pipeline_id = pipeline_id
        else:
            pipeline_id = self._heavy_pipeline_id or "ui-progress"
        # Sidebar STATUS totals come from folder-info polls — never overwrite
        # them with partial Camel / folder-index counts (#208 confusion).
        server = self._mail.get_folder_status_totals(account_uid, folder_name)
        server_total = server[1] if server is not None else -1
        from post.mail.folder_status_cache import status_total_is_trusted

        keep_indexing = not progress.done
        if not progress.done:
            if (
                status_total_is_trusted(folder_name, server_total)
                and server_total > indexed
            ):
                self._set_status(
                    f"Fetching {folder_name} headers… {indexed} of "
                    f"{server_total} on server"
                )
            else:
                self._set_status(
                    f"Fetching {folder_name} headers… {indexed} indexed so far"
                )
            self._message_sync_in_progress = True
        else:
            from post.mail.folder_status_cache import index_caught_up

            # Indexer may finish after a partial Graph refresh while STATUS is
            # still much larger — keep chasing while this folder stays open.
            if status_total_is_trusted(
                folder_name, server_total
            ) and not index_caught_up(indexed, server_total, folder_name):
                keep_indexing = True
                self._message_sync_in_progress = True
                self._set_status(
                    f"Fetching {folder_name} headers… {indexed} of "
                    f"{server_total} on server"
                )

                def _retry_heavy_refresh() -> bool:
                    if load_id != self._messages_load_generation:
                        return False
                    if self._current_folder != folder_name:
                        return False
                    # Preserve prepare/seed flags — a bare cursor re-runs
                    # prepare_content_refresh and wipes M365 sync progress.
                    # Exception: incomplete-delta keep-alive must allow prepare
                    # so a stale/empty Graph cursor can be reset (#208).
                    retry_cursor = dict(progress.cursor or {})
                    force_incomplete = bool(
                        retry_cursor.get("force_prepare_incomplete_delta")
                    )
                    retry_cursor.update(
                        {
                            "refresh_done": False,
                            "pending_server_refresh": True,
                            "status_seeded": True,
                            "indexed_after_refresh": indexed,
                        }
                    )
                    if force_incomplete:
                        retry_cursor["did_prepare_content_refresh"] = False
                        retry_cursor["force_prepare_incomplete_delta"] = True
                    else:
                        retry_cursor["did_prepare_content_refresh"] = True
                    log.debug(
                        "Heavy-folder UI retry catch-up load_id=%s %s/%s "
                        "indexed=%d server_total=%d force_incomplete=%s",
                        load_id,
                        account_uid,
                        folder_name,
                        indexed,
                        server_total,
                        force_incomplete,
                    )
                    self._start_background_heavy_folder_index(
                        load_id,
                        account_uid,
                        folder_name,
                        cursor=retry_cursor,
                    )
                    return False

                GLib.timeout_add(2000, _retry_heavy_refresh)
            else:
                self._message_sync_in_progress = False
                if self._heavy_index_in_progress == (account_uid, folder_name):
                    self._heavy_index_in_progress = None

        current = self._current_folder_messages or []
        # Always keep the in-memory list current so scroll-to-load-more can reach
        # newly indexed *older* headers (Graph backfill rarely changes the top).
        grew = indexed > len(current)
        top_unchanged = (
            bool(current)
            and bool(messages)
            and message_list_fingerprint(current[:MESSAGE_LIST_UI_BIND_CAP])
            == message_list_fingerprint(messages[:MESSAGE_LIST_UI_BIND_CAP])
        )
        store_n = self._message_list_view.item_count()
        log.debug(
            "Heavy-folder UI progress load_id=%s %s/%s indexed=%d "
            "prev=%d server_total=%d done=%s keep=%s grew=%s "
            "top_unchanged=%s bound=%d gtk_store=%d pipeline_id=%s",
            load_id,
            account_uid,
            folder_name,
            indexed,
            len(current),
            server_total,
            progress.done,
            keep_indexing,
            grew,
            top_unchanged,
            self._message_list_bound_count,
            store_n,
            pipeline_id,
        )

        if message_lists_equivalent_for_ui(
            current,
            messages,
            current_total=self._message_total,
            refreshed_total=indexed,
        ):
            will_catchup = indexed > self._message_list_bound_count > 0
            _log_heavy_pipeline(
                "list",
                account_uid,
                folder_name,
                pipeline_id=pipeline_id,
                action="skip_rebind_equivalent",
                load_id=load_id,
                indexed=indexed,
                bound=self._message_list_bound_count,
                gtk_store=store_n,
                catchup_scheduled=will_catchup,
                visible=(
                    "no_top_change_catchup_binds_older"
                    if will_catchup
                    else "no_list_change_expected"
                ),
            )
            log.debug(
                "Heavy-folder UI skip rebind (equivalent) %s/%s indexed=%d "
                "bound=%d gtk_store=%d",
                account_uid,
                folder_name,
                indexed,
                self._message_list_bound_count,
                store_n,
            )
            # Index may have grown earlier while we kept the top rows; bind the
            # unbound older rows so the list length grows without a top rebuild.
            if will_catchup:
                self._current_folder_messages = messages
                self._message_total = indexed
                self._update_message_status(account, folder_name)
                self._schedule_bind_unbound_heavy_messages(load_id)
            if not keep_indexing:
                GLib.idle_add(self._on_messages_sync_finished, load_id, False)
                self._update_message_status(account, folder_name)
            return False

        self._current_folder_messages = messages
        self._message_total = indexed
        self._message_list_source = "local"
        if not messages:
            self._message_empty_label.set_label(f"No Messages in {folder_name}")
            self._message_stack.set_visible_child_name("empty")
            if not keep_indexing:
                self._update_message_status(account, folder_name)
                GLib.idle_add(self._on_messages_sync_finished, load_id, False)
            return False

        self._message_stack.set_visible_child_name("list")

        if grew and top_unchanged and self._message_list_bound_count > 0:
            # Older headers appended below the visible newest window — do not
            # rebuild Gtk (scroll jump). Bind the new rows below so scroll can
            # reach them even while the user stays at the top (#208).
            _log_heavy_pipeline(
                "list",
                account_uid,
                folder_name,
                pipeline_id=pipeline_id,
                action="keep_top_bind_older",
                load_id=load_id,
                indexed=indexed,
                bound=self._message_list_bound_count,
                gtk_store=store_n,
                visible="top_unchanged_scroll_down_for_new_rows",
            )
            log.debug(
                "Heavy-folder UI keep top rows; binding older mail below "
                "%s/%s indexed=%d bound=%d gtk_store=%d",
                account_uid,
                folder_name,
                indexed,
                self._message_list_bound_count,
                store_n,
            )
            self._update_message_status(account, folder_name)
            self._schedule_bind_unbound_heavy_messages(load_id)
            if keep_indexing and status_total_is_trusted(
                folder_name, server_total
            ):
                self._set_status(
                    f"Fetching {folder_name} headers… {indexed} of "
                    f"{server_total} on server · older mail loading "
                    f"(scroll down for more)"
                )
            elif keep_indexing:
                self._set_status(
                    f"Fetching {folder_name} headers… {indexed} indexed · "
                    f"older mail loading (scroll down for more)"
                )
            return False

        bind_cap = min(indexed, MESSAGE_LIST_UI_BIND_CAP)
        _log_heavy_pipeline(
            "list",
            account_uid,
            folder_name,
            pipeline_id=pipeline_id,
            action="rebind_list",
            load_id=load_id,
            indexed=indexed,
            bind_cap=bind_cap,
            prev_bound=self._message_list_bound_count,
            prev_gtk_store=store_n,
            visible="top_may_change_full_rebind",
        )
        log.debug(
            "Heavy-folder UI rebind list %s/%s indexed=%d",
            account_uid,
            folder_name,
            indexed,
        )

        def _after_rebind() -> None:
            if load_id != self._messages_load_generation:
                return
            after_store = self._message_list_view.item_count()
            _log_heavy_pipeline(
                "list",
                account_uid,
                folder_name,
                pipeline_id=pipeline_id,
                action="rebind_complete",
                load_id=load_id,
                indexed=indexed,
                bound=self._message_list_bound_count,
                gtk_store=after_store,
                visible=(
                    "rows_in_gtk_store"
                    if after_store > 0
                    else "gtk_store_empty"
                ),
            )
            if keep_indexing:
                self._schedule_bind_unbound_heavy_messages(load_id)
            else:
                self._on_messages_sync_finished(load_id, False)

        self._apply_messages_to_list(
            messages,
            folder_name,
            account=account,
            load_id=load_id,
            on_complete=_after_rebind,
        )
        return False

    @staticmethod
    def _load_source_label(source: str) -> str:
        if source == "server":
            return "from server"
        if source in {"disk_cache", "local", "memory"}:
            return "from local cache"
        if source == "outbox":
            return "from Outbox"
        return ""

    def _predict_initial_load_source(
        self,
        account_uid: str,
        folder_name: str,
        *,
        viewing_outbox: bool,
        should_sync: bool,
        use_background_sync: bool,
        force_sync: bool = False,
    ) -> str:
        if viewing_outbox:
            return "outbox"
        if force_sync and should_sync:
            return "server"
        if use_background_sync:
            return "disk_cache" if folder_index_has_cache(account_uid, folder_name) else "local"
        if should_sync:
            return "server"
        if folder_index_has_cache(account_uid, folder_name):
            return "disk_cache"
        return "local"

    def _loading_progress_text(
        self,
        display_folder: str,
        *,
        searching: bool,
        source: str,
    ) -> str:
        action = "Searching" if searching else "Loading"
        target = self._search_target_label(display_folder) if searching else display_folder
        if searching:
            return f"Searching {target}…"
        detail = self._load_source_label(source)
        if detail:
            return f"{action} {target} {detail}…"
        return f"{action} {target}…"

    def _reset_search_progress_ui(self) -> None:
        self._message_loading_progress.set_visible(False)
        self._message_loading_progress.set_fraction(0.0)
        self._message_loading_spinner.set_visible(True)
        self._search_progress_last_ui_time = 0
        self._hide_status_search_progress()

    def _hide_status_search_progress(self) -> None:
        if self._status_progress_pulse_id is not None:
            GLib.source_remove(self._status_progress_pulse_id)
            self._status_progress_pulse_id = None
        self._status_progress.remove_css_class("status-search-progress-indeterminate")
        self._status_progress.set_visible(False)
        self._status_progress.set_fraction(0.0)

    def _status_search_progress_pulse(self) -> bool:
        if not self._status_progress.get_visible():
            self._status_progress_pulse_id = None
            return False
        self._status_progress.pulse()
        return True

    def _show_status_search_progress(
        self, *, fraction: float = 0.0, indeterminate: bool = False
    ) -> None:
        self._status_progress.set_visible(True)
        if indeterminate:
            if self._status_progress_pulse_id is None:
                self._status_progress_pulse_id = GLib.timeout_add(
                    80, self._status_search_progress_pulse
                )
            self._status_progress.add_css_class("status-search-progress-indeterminate")
            self._status_progress.set_fraction(0.0)
            return
        if self._status_progress_pulse_id is not None:
            GLib.source_remove(self._status_progress_pulse_id)
            self._status_progress_pulse_id = None
        self._status_progress.remove_css_class("status-search-progress-indeterminate")
        self._status_progress.set_fraction(fraction)

    def _show_search_index_loading_ui(self, load_id: int, display_folder: str) -> None:
        if load_id != self._messages_load_generation:
            return
        if self._search_query is None:
            return
        label = f"Loading Index for {display_folder}…"
        self._show_status_search_progress(indeterminate=True)
        self._set_status(label)

    def _report_search_progress(
        self, load_id: int, progress: SearchFilterProgress
    ) -> None:
        schedule_on_gtk_main(self._apply_search_progress, load_id, progress)

    def _apply_search_progress(
        self, load_id: int, progress: SearchFilterProgress
    ) -> bool:
        if self._is_closing:
            return False
        if load_id != self._messages_load_generation:
            return False
        if self._search_query is None:
            return False
        now = GLib.get_monotonic_time()
        if (
            progress.scanned < progress.message_count
            and now - self._search_progress_last_ui_time
            < _SEARCH_PROGRESS_UI_INTERVAL_US
        ):
            return False
        self._search_progress_last_ui_time = now
        progress_text = format_search_filter_progress(progress)
        fraction = search_filter_progress_fraction(progress)
        self._show_status_search_progress(fraction=fraction)
        self._set_status(progress_text)
        return False

    def _report_search_matches(self, load_id: int, batch: list[dict]) -> None:
        schedule_on_gtk_main(self._apply_search_matches, load_id, batch)

    def _apply_search_matches(self, load_id: int, batch: list[dict]) -> bool:
        if self._is_closing:
            return False
        if load_id != self._messages_load_generation:
            return False
        if self._search_query is None or not batch:
            return False
        account = self._current_account
        folder_name = self._current_folder
        if account is None or folder_name is None:
            return False

        first_batch = not self._search_results_streamed
        self._search_results_streamed = True
        if first_batch:
            self._update_search_scope_ui()
            self._message_stack.set_visible_child_name("list")

        annotated_batch = [
            annotate_search_match(
                message,
                account_uid=account.uid,
                folder_name=folder_name,
            )
            if not message.get("_search_row_key")
            else message
            for message in batch
        ]
        self._message_list_view.insert_messages_newest_first(
            annotated_batch, folder_name=folder_name
        )
        if self._current_folder_messages is None:
            self._current_folder_messages = []
        insert_messages_newest_first(self._current_folder_messages, annotated_batch)
        return False

    def _should_use_cached_header_search(
        self,
        search_query: MessageSearchQuery | None,
        account_uid: str,
        folder_name: str,
    ) -> bool:
        """True when header-only search can use the folder-index (RAM ∪ disk).

        Account / All Mail scopes must use ``_start_mail_search`` so the
        selected scope is respected (#173). Search unions grow-only disk
        with RAM so a smaller Camel summary cannot hide indexed headers (#363).
        """
        return (
            search_query is not None
            and self._search_scope.kind == SEARCH_SCOPE_FOLDER
            and folder_index_has_cache(account_uid, folder_name)
            and not query_requires_body_scan(search_query)
        )

    def _begin_chunked_cached_header_search(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        folder_display: str,
        snapshot: tuple[list[dict], int, int] | None,
        fallback_worker: Callable[[], None],
    ) -> None:
        if load_id != self._messages_load_generation:
            return
        if self._is_closing:
            return
        if snapshot is None:
            self._mail.cancel_folder_refresh()
            get_mail_io_thread().submit_front(fallback_worker)
            return
        search_query = self._search_query
        if search_query is None:
            return

        cached_messages, cached_unread, _cached_total = snapshot
        state = {"offset": 0, "matched": []}
        account_label, folder_label = self._search_folder_scope_labels(
            folder_display,
            account_uid=account_uid,
        )

        def search_progress(
            scanned: int, message_count: int, matches: int
        ) -> SearchFilterProgress:
            return SearchFilterProgress(
                scanned,
                message_count,
                matches,
                account_label=account_label,
                folder_label=folder_label,
            )

        def is_search_cancelled() -> bool:
            return (
                self._is_closing
                or load_id != self._messages_load_generation
                or self._search_query is not search_query
            )

        def process_chunk() -> bool:
            if is_search_cancelled():
                return False
            start = state["offset"]
            end = min(
                start + _CACHED_HEADER_SEARCH_CHUNK_SIZE,
                len(cached_messages),
            )
            chunk_matched = filter_messages_by_query(
                list(cached_messages[start:end]),
                search_query,
                is_cancelled=is_search_cancelled,
                on_progress=lambda progress: self._apply_search_progress(
                    load_id,
                    search_progress(
                        start + progress.scanned,
                        len(cached_messages),
                        len(state["matched"]) + progress.matches,
                    ),
                ),
                on_matches=lambda batch: self._apply_search_matches(load_id, batch),
            )
            state["matched"].extend(chunk_matched)
            state["offset"] = end
            self._apply_search_progress(
                load_id,
                search_progress(end, len(cached_messages), len(state["matched"])),
            )
            if end < len(cached_messages):
                return True
            if is_search_cancelled():
                return False
            self._on_messages_loaded(
                load_id,
                account_uid,
                folder_name,
                sort_messages_newest_first(state["matched"]),
                cached_unread,
                len(state["matched"]),
                "disk_cache",
                False,
                None,
            )
            return False

        if cached_messages:
            self._apply_search_progress(
                load_id,
                search_progress(0, len(cached_messages), 0),
            )
        GLib.idle_add(process_chunk)

    def _message_load_status_detail(self) -> str:
        parts: list[str] = []
        if (
            not self._network_available
            and self._message_list_source in {"disk_cache", "local", "memory"}
            and (self._message_list_view.item_count() > 0 or self._message_total > 0)
        ):
            parts.append(OFFLINE_CACHED_LIST_STATUS)
        if self._message_sync_in_progress:
            source_label = self._load_source_label(self._message_list_source)
            if source_label:
                parts.append(source_label)
            parts.append(MESSAGE_LIST_SYNC_STATUS)
        return " · ".join(parts)

    def _with_load_status_detail(self, text: str) -> str:
        detail = self._message_load_status_detail()
        if detail:
            return f"{text} · {detail}"
        return text

    def _start_mail_search(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        query: MessageSearchQuery,
        scope: SearchScope,
        *,
        sync_pending: bool,
    ) -> None:
        def on_progress(progress: SearchFilterProgress) -> None:
            self._report_search_progress(load_id, progress)

        def on_matches(batch: list[dict]) -> None:
            self._report_search_matches(load_id, batch)

        def on_complete(result: tuple[list[dict], int, int, str]) -> None:
            messages, unread, total, source = result
            GLib.idle_add(
                self._on_messages_loaded,
                load_id,
                account_uid,
                folder_name,
                messages,
                unread,
                total,
                source,
                sync_pending,
                None,
            )

        kwargs = {
            "on_progress": on_progress,
            "on_matches": on_matches,
            "on_complete": on_complete,
        }
        if scope.kind == SEARCH_SCOPE_ALL:
            self._mail.start_search_all_messages(query, **kwargs)
        elif scope.kind == SEARCH_SCOPE_ACCOUNT:
            self._mail.start_search_account_messages(
                scope.account_uid or account_uid,
                query,
                **kwargs,
            )
        else:
            self._mail.start_search_folder_messages(
                account_uid,
                folder_name,
                query,
                **kwargs,
            )

    def _load_messages(
        self,
        account_uid: str,
        folder_name: str,
        *,
        offset: int = 0,
        sync: bool | None = None,
        force_sync: bool = False,
        skip_disk_cache: bool = False,
        seed_search_matches: list[dict] | None = None,
    ) -> None:
        account = self._current_account
        if account is None or account.uid != account_uid:
            return
        if offset != 0:
            return

        if sync is None:
            sync = self._account_server_sync_enabled(account_uid)
        if not self._network_available:
            sync = False

        # Same Archive/etc. already indexing: do not bump generation, clear the
        # list, or start a Camel summary rebuild — that cancels Graph mid-flight
        # and can collapse thousands of indexed headers back to ~1.3k (#208).
        if (
            self._search_query is None
            and not force_sync
            and not skip_disk_cache
            and not is_post_outbox_folder(folder_name)
            and is_heavy_folder_name(folder_name)
            and self._heavy_index_in_progress == (account_uid, folder_name)
            and self._current_folder == folder_name
            and self._current_account is not None
            and self._current_account.uid == account_uid
        ):
            log.debug(
                "Heavy-folder UI ignore reload while indexing %s/%s "
                "(bound=%d indexed=%d)",
                account_uid,
                folder_name,
                self._message_list_bound_count,
                len(self._current_folder_messages or []),
            )
            self._hold_offline_sync_for_folder_work(
                self._messages_load_generation
            )
            if self._current_folder_messages:
                self._schedule_bind_unbound_heavy_messages(
                    self._messages_load_generation
                )
            return

        self._messages_load_generation += 1
        load_id = self._messages_load_generation
        self._messages_load_expects_search = self._search_query is not None
        self._heavy_bind_catchup_load_id = None
        self._heavy_pipeline_id = None

        # Abort background folder-info REFRESH / refresh_info_sync so the new
        # folder load is not stuck behind Camel I/O (#170 hang on switch).
        # Do not cancel sidebar tree list_folders — that thrash-cancels M365
        # into permanent "Loading folders…".
        self._mail.cancel_folder_search()
        self._mail.cancel_folder_refresh()
        # Cancel an in-flight Archive/etc. Graph refresh when navigating away
        # from the folder being indexed. Do not use ``_current_folder`` here —
        # ``_prepare_folder_selection`` already updated it to the new folder
        # before ``_load_messages`` runs (#208).
        indexing = self._heavy_index_in_progress
        if indexing is not None:
            idx_account, idx_folder = indexing
            leaving_indexed = (
                idx_account != account_uid or idx_folder != folder_name
            )
            if leaving_indexed or not is_heavy_folder_name(folder_name):
                self._mail.cancel_heavy_folder_index_refresh()
                if leaving_indexed:
                    self._mail.clear_heavy_folder_index_session(
                        idx_account, idx_folder
                    )
                self._heavy_index_in_progress = None
        self._sidebar.cancel_folder_count_poll()
        # Pause Gmail/etc. offline backfill so this folder owns mail I/O + status.
        self._hold_offline_sync_for_folder_work(load_id)

        display_folder = (
            "Outbox"
            if is_post_outbox_folder(folder_name)
            else (
                self._sidebar.folder_display_name(account_uid, folder_name)
                if force_sync
                else folder_name
            )
        )
        search_query = self._search_query
        seed_matches = (
            list(seed_search_matches)
            if search_query is not None and seed_search_matches
            else None
        )
        if search_query is not None:
            self._search_results_streamed = bool(seed_matches)
            if (
                self._pre_search_snapshot is not None
                and self._pre_search_folder == (account_uid, folder_name)
                and self._mail.get_folder_index_snapshot(account_uid, folder_name)
                is None
            ):
                pre_messages, pre_unread, pre_total, _pre_source = (
                    self._pre_search_snapshot
                )
                if pre_messages:
                    total = (
                        pre_total
                        if pre_total >= 0
                        else len(pre_messages)
                    )
                    self._mail.seed_folder_index(
                        account_uid,
                        folder_name,
                        pre_messages,
                        pre_unread,
                        total,
                    )
        self._mail.cancel_folder_search()
        viewing_outbox = is_post_outbox_folder(folder_name)
        should_sync = sync
        use_background_sync = (
            should_sync
            and self._network_available
            and not viewing_outbox
            and search_query is None
            and not force_sync
        )
        from_label = account.email or account.display_label

        def fetch_messages(sync_flag: bool) -> tuple[list[dict], int, int, str]:
            if viewing_outbox:
                messages, unread, total = list_queued_messages(
                    account_uid,
                    from_label=from_label,
                )
                return messages, unread, total, "outbox"
            messages, unread, total, source = self._mail.get_folder_messages(
                account_uid,
                folder_name,
                sync=sync_flag,
            )
            return messages, unread, total, source

        self._message_total = -1
        self._current_folder_messages = None
        self._message_list_bound_count = 0
        self._message_sync_in_progress = False
        initial_source = self._predict_initial_load_source(
            account_uid,
            folder_name,
            viewing_outbox=viewing_outbox,
            should_sync=should_sync,
            use_background_sync=use_background_sync,
            force_sync=force_sync,
        )
        self._message_list_source = initial_source
        if (
            force_sync
            and should_sync
            and self._network_available
            and not viewing_outbox
            and search_query is None
        ):
            loading_label = f"Refreshing {display_folder} From Server…"
        else:
            loading_label = self._loading_progress_text(
                display_folder,
                searching=search_query is not None,
                source=initial_source,
            )
        self._set_status(loading_label)
        if (
            not self._pending_restore_message_uid
            and self._current_message_uid
            and self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
        ):
            self._pending_restore_message_uid = self._current_message_uid
            self._restore_message_folder = (account_uid, folder_name)
        self._message_popover.popdown()
        self._message_list_view.clear()
        if seed_matches:
            seed_matches = sort_messages_newest_first(seed_matches)
            seed_keys = {self._message_list_key(message) for message in seed_matches}
            if (
                self._current_message_uid
                and self._current_message_uid not in seed_keys
            ):
                self._clear_reader()
            self._current_folder_messages = seed_matches
            self._message_total = len(seed_matches)
            self._message_list_view.set_messages(
                seed_matches,
                folder_name=folder_name,
            )
            self._message_stack.set_visible_child_name("list")
        else:
            self._clear_reader()

        disk_cache_eligible = (
            not skip_disk_cache
            and not viewing_outbox
            and not force_sync
            and search_query is None
        )
        has_disk_cache = (
            disk_cache_eligible
            and folder_index_has_cache(account_uid, folder_name)
        )

        self._reset_search_progress_ui()
        self._message_loading_label.set_label(loading_label)
        if search_query is not None:
            self._message_stack.set_visible_child_name("list")
            if self._mail.get_folder_index_snapshot(account_uid, folder_name) is None:
                self._show_search_index_loading_ui(load_id, display_folder)
            else:
                self._show_status_search_progress(fraction=0.0)
        else:
            self._message_loading_spinner.start()
            self._message_stack.set_visible_child_name("loading")

        send_pending = self._mail.outbound_sends_pending()
        defer_mail_io = (
            send_pending
            and not has_disk_cache
            and search_query is None
        )
        sync_after_send = use_background_sync and send_pending

        def schedule_background_sync() -> None:
            # Heavy folders (Archive/Trash/Junk) use a chunked preemptible
            # indexer instead of a full uncapped refresh_info reindex (#208).
            if use_background_sync and is_heavy_folder_name(folder_name):
                if self._heavy_index_in_progress == (account_uid, folder_name):
                    # Same folder already indexing — do not start a second
                    # refresh_info (and do not prepare_content_refresh again).
                    log.debug(
                        "Heavy-folder UI skip duplicate indexer %s/%s "
                        "load_id=%s",
                        account_uid,
                        folder_name,
                        load_id,
                    )
                    return
                if not send_pending:
                    self._start_background_heavy_folder_index(
                        load_id, account_uid, folder_name
                    )
                elif sync_after_send:

                    def heavy_after_send() -> None:
                        if load_id != self._messages_load_generation:
                            return
                        if self._heavy_index_in_progress == (
                            account_uid,
                            folder_name,
                        ):
                            return
                        self._start_background_heavy_folder_index(
                            load_id, account_uid, folder_name
                        )

                    self._mail.when_outbound_sends_complete(heavy_after_send)
                return
            if use_background_sync and not send_pending:
                self._start_background_message_sync(
                    load_id,
                    account_uid,
                    folder_name,
                    fetch_messages,
                )
            elif sync_after_send:

                def sync_after_send_cb() -> None:
                    if load_id != self._messages_load_generation:
                        return
                    self._start_background_message_sync(
                        load_id,
                        account_uid,
                        folder_name,
                        fetch_messages,
                    )

                self._mail.when_outbound_sends_complete(sync_after_send_cb)

        def worker_initial() -> None:
            if load_id != self._messages_load_generation:
                search_trace(
                    "search_worker_skip",
                    load_id=load_id,
                    generation=self._messages_load_generation,
                )
                GLib.idle_add(self._stop_superseded_message_loading, load_id)
                return
            if search_query is not None:
                search_trace(
                    "search_worker_start",
                    load_id=load_id,
                    searching=True,
                    path="incremental",
                )
                self._start_mail_search(
                    load_id,
                    account_uid,
                    folder_name,
                    search_query,
                    self._search_scope,
                    sync_pending=use_background_sync and not send_pending,
                )
                return
            search_trace(
                "search_worker_start",
                load_id=load_id,
                searching=search_query is not None,
                initial_sync=should_sync and not use_background_sync,
            )
            error: Exception | None = None
            messages: list[dict] | None = None
            unread = -1
            total = -1
            source = initial_source
            initial_sync = should_sync and not use_background_sync
            try:
                with search_trace_timer(
                    "search_fetch",
                    load_id=load_id,
                    searching=search_query is not None,
                ):
                    messages, unread, total, source = fetch_messages(initial_sync)
            except Exception as exc:
                if (
                    not viewing_outbox
                    and initial_sync
                    and is_network_unavailable_error(exc)
                ):
                    try:
                        messages, unread, total, source = fetch_messages(False)
                    except Exception as retry_exc:
                        log_mail_error(log, "Failed to list messages", retry_exc)
                        error = retry_exc
                else:
                    log_mail_error(log, "Failed to list messages", exc)
                    error = exc
            search_trace(
                "search_worker_idle_add",
                load_id=load_id,
                searching=search_query is not None,
                match_count=len(messages) if messages is not None else None,
                error=repr(error) if error is not None else None,
            )
            GLib.idle_add(
                self._on_messages_loaded,
                load_id,
                account_uid,
                folder_name,
                messages,
                unread,
                total,
                source,
                use_background_sync and not send_pending,
                error,
            )

        def start_initial_worker() -> None:
            if self._should_use_cached_header_search(
                search_query, account_uid, folder_name
            ):
                def worker_load_cached_header_index() -> None:
                    if load_id != self._messages_load_generation:
                        schedule_on_gtk_main(
                            self._stop_superseded_message_loading, load_id
                        )
                        return
                    search_trace(
                        "search_worker_start",
                        load_id=load_id,
                        path="disk_cache_headers",
                    )
                    try:
                        snapshot = self._mail.get_folder_index_for_search(
                            account_uid, folder_name
                        )
                    except Exception as exc:
                        schedule_on_gtk_main(
                            self._on_messages_loaded,
                            load_id,
                            account_uid,
                            folder_name,
                            None,
                            -1,
                            -1,
                            initial_source,
                            False,
                            exc,
                        )
                        return
                    if load_id != self._messages_load_generation:
                        return
                    schedule_on_gtk_main(
                        self._begin_chunked_cached_header_search,
                        load_id,
                        account_uid,
                        folder_name,
                        display_folder,
                        snapshot,
                        worker_initial,
                    )

                get_mail_io_thread().submit_front(worker_load_cached_header_index)
                return
            if search_query is not None:
                self._mail.cancel_folder_refresh()
            search_trace("search_worker_submit_front", load_id=load_id)
            get_mail_io_thread().submit_front(worker_initial)

        def worker_cache() -> None:
            if load_id != self._messages_load_generation:
                return
            snapshot = load_folder_index_cache(account_uid, folder_name)
            if snapshot is not None:
                cached_messages, cached_unread, cached_total = snapshot
                self._mail.seed_folder_index(
                    account_uid,
                    folder_name,
                    cached_messages,
                    cached_unread,
                    cached_total,
                )
                search_trace(
                    "search_index_seeded",
                    account=account_uid,
                    folder=folder_name,
                    message_count=len(cached_messages),
                )

            def on_main() -> bool:
                if load_id != self._messages_load_generation:
                    return False
                if snapshot is not None:
                    cached_messages, cached_unread, cached_total = snapshot

                    def after_list() -> None:
                        if self._search_query is None:
                            self._try_restore_selected_message(account_uid, folder_name)
                        schedule_background_sync()

                    self._on_messages_loaded(
                        load_id,
                        account_uid,
                        folder_name,
                        list(cached_messages),
                        cached_unread,
                        cached_total,
                        "disk_cache",
                        use_background_sync and not send_pending,
                        None,
                        after_list,
                    )
                elif defer_mail_io:

                    def start_after_send() -> None:
                        if load_id != self._messages_load_generation:
                            return
                        start_initial_worker()

                    self._mail.when_outbound_sends_complete(start_after_send)
                else:
                    start_initial_worker()
                return False

            GLib.idle_add(on_main)

        if has_disk_cache:
            get_mail_io_thread().submit(worker_cache)
        else:
            if defer_mail_io:
                search_trace(
                    "search_load_defer_send",
                    load_id=load_id,
                    searching=search_query is not None,
                )

                def start_after_send() -> None:
                    if load_id != self._messages_load_generation:
                        return
                    start_initial_worker()

                self._mail.when_outbound_sends_complete(start_after_send)
            else:
                search_trace(
                    "search_load_start_worker",
                    load_id=load_id,
                    searching=search_query is not None,
                    path="direct",
                )
                start_initial_worker()
            schedule_background_sync()

        search_trace(
            "search_load_scheduled",
            load_id=load_id,
            searching=search_query is not None,
            has_disk_cache=has_disk_cache,
            defer_mail_io=defer_mail_io,
            send_pending=send_pending,
            expects_search=self._messages_load_expects_search,
            generation=self._messages_load_generation,
        )

    def _on_messages_sync_finished(self, load_id: int, changed: bool) -> bool:
        if load_id != self._messages_load_generation:
            return False
        self._message_sync_in_progress = False
        account = self._current_account
        folder = self._current_folder
        # Keep ``_heavy_index_in_progress`` until catch-up or leave; the release
        # path still holds offline sync while a heavy folder stays selected.
        self._release_offline_sync_for_folder_work(load_id)
        if changed:
            return False
        if account is not None and folder is not None:
            self._update_message_status(account, folder)
        return False

    def _stop_superseded_message_loading(self, load_id: int) -> bool:
        if load_id >= self._messages_load_generation:
            return False
        if self._message_stack.get_visible_child_name() != "loading":
            return False
        search_trace(
            "search_loading_spinner_stop",
            load_id=load_id,
            generation=self._messages_load_generation,
            reason="superseded",
        )
        self._message_loading_spinner.stop()
        return False

    def _on_messages_loaded(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        messages: list[dict] | None,
        unread: int,
        total: int,
        source: str,
        sync_pending: bool,
        error: Exception | None,
        after_list_complete: Callable[[], None] | None = None,
    ) -> bool:
        if load_id != self._messages_load_generation:
            search_trace(
                "search_loaded_drop",
                load_id=load_id,
                generation=self._messages_load_generation,
                reason="stale_load_id",
            )
            self._stop_superseded_message_loading(load_id)
            return False
        if self._messages_load_expects_search != (self._search_query is not None):
            search_trace(
                "search_loaded_drop",
                load_id=load_id,
                generation=self._messages_load_generation,
                reason="search_expectation_mismatch",
                expects_search=self._messages_load_expects_search,
                has_query=self._search_query is not None,
            )
            self._stop_superseded_message_loading(load_id)
            return False

        self._message_loading_spinner.stop()
        self._reset_search_progress_ui()

        if error is not None:
            if is_network_unavailable_error(error):
                if (
                    not is_post_outbox_folder(folder_name)
                    and folder_index_has_cache(account_uid, folder_name)
                ):
                    search_query = self._search_query
                    search_scope = self._search_scope

                    def cache_worker() -> None:
                        if load_id != self._messages_load_generation:
                            return
                        if search_query is not None:
                            try:
                                self._start_mail_search(
                                    load_id,
                                    account_uid,
                                    folder_name,
                                    search_query,
                                    search_scope,
                                    sync_pending=False,
                                )
                            except Exception:
                                GLib.idle_add(
                                    self._on_messages_loaded,
                                    load_id,
                                    account_uid,
                                    folder_name,
                                    None,
                                    -1,
                                    -1,
                                    "disk_cache",
                                    False,
                                    None,
                                )
                            return
                        cached_messages: list[dict] | None = None
                        cached_unread = -1
                        cached_total = -1
                        cached_source = "disk_cache"
                        try:
                            (
                                cached_messages,
                                cached_unread,
                                cached_total,
                                cached_source,
                            ) = self._mail.get_folder_messages(
                                account_uid,
                                folder_name,
                                sync=False,
                            )
                        except Exception:
                            cached_messages = None
                        GLib.idle_add(
                            self._on_messages_loaded,
                            load_id,
                            account_uid,
                            folder_name,
                            cached_messages,
                            cached_unread,
                            cached_total,
                            cached_source,
                            False,
                            None,
                        )

                    get_mail_io_thread().submit(cache_worker)
                    return False
                self._message_empty_label.set_label(OFFLINE_MAIL_MESSAGE)
                self._message_stack.set_visible_child_name("empty")
            else:
                self._message_error_label.set_label(str(error))
                self._message_stack.set_visible_child_name("error")
                show_error_toast(self, f"Could not load {folder_name}")
            self._release_offline_sync_for_folder_work(load_id)
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            self._message_stack.set_visible_child_name("list")
            self._release_offline_sync_for_folder_work(load_id)
            return False

        if not self._search_query:
            if is_post_outbox_folder(folder_name):
                self._sidebar.refresh_outbox_row(account_uid)
            elif not is_heavy_folder_name(folder_name):
                # Heavy folders: keep sidebar on STATUS high-water, not Camel
                # summary / folder-index counts from this load (#208).
                self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        if not messages:
            if self._search_query is not None and (
                self._search_results_streamed
                or self._message_list_view.item_count() > 0
            ):
                # Search was cancelled after partial streaming (e.g. opening a result).
                self._message_sync_in_progress = sync_pending
                self._message_list_source = source
                if self._current_folder_messages:
                    self._message_total = len(self._current_folder_messages)
                self._hide_status_search_progress()
                self._message_stack.set_visible_child_name("list")
                self._update_message_status(account, folder_name)
                search_trace(
                    "search_loaded_complete",
                    load_id=load_id,
                    match_count=self._message_list_view.item_count(),
                    view="list",
                    searching=True,
                    streamed=True,
                    reason="preserve_streamed_results",
                )
                if after_list_complete is not None:
                    after_list_complete()
                if not sync_pending:
                    self._release_offline_sync_for_folder_work(load_id)
                return False

            self._current_folder_messages = messages
            self._message_total = total
            self._message_list_source = source
            self._message_sync_in_progress = sync_pending
            folder_label = "Outbox" if is_post_outbox_folder(folder_name) else folder_name
            if is_post_outbox_folder(folder_name):
                self._message_empty_label.set_label("No Queued Messages")
            elif self._search_query is not None:
                self._message_empty_label.set_label(
                    self._search_empty_label(folder_label)
                )
            else:
                self._message_empty_label.set_label(f"No Messages in {folder_label}")
            self._message_stack.set_visible_child_name("empty")
            self._update_message_status(account, folder_name)
            search_trace(
                "search_loaded_complete",
                load_id=load_id,
                match_count=0,
                view="empty",
                searching=True,
            )
            # Empty disk cache still needs after_list (background refresh) (#339).
            if after_list_complete is not None:
                after_list_complete()
            if not sync_pending:
                self._release_offline_sync_for_folder_work(load_id)
            return False

        self._current_folder_messages = messages
        self._message_total = total
        self._message_list_source = source
        self._message_sync_in_progress = sync_pending

        if self._search_query is not None and self._search_results_streamed:
            def streamed_after_list() -> None:
                if self._search_query is None:
                    self._try_restore_selected_message(account.uid, folder_name)

            on_complete = after_list_complete or streamed_after_list
            search_trace(
                "search_loaded_complete",
                load_id=load_id,
                match_count=len(messages),
                view="list",
                searching=True,
                streamed=True,
            )
            self._message_stack.set_visible_child_name("list")
            self._update_message_status(account, folder_name)
            on_complete()
            if not sync_pending:
                self._release_offline_sync_for_folder_work(load_id)
            return False

        self._message_stack.set_visible_child_name("list")

        def default_after_list() -> None:
            if self._search_query is None:
                self._try_restore_selected_message(account.uid, folder_name)

        on_complete = after_list_complete or default_after_list
        search_trace(
            "search_loaded_complete",
            load_id=load_id,
            match_count=len(messages),
            view="list",
            searching=self._search_query is not None,
        )
        self._apply_messages_to_list(
            messages,
            folder_name,
            account=account,
            load_id=load_id,
            on_complete=on_complete,
        )
        if not sync_pending:
            self._release_offline_sync_for_folder_work(load_id)
        return False

    def _on_messages_refreshed(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        messages: list[dict] | None,
        unread: int,
        total: int,
        error: Exception | None,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False
        if self._search_query is not None:
            return False
        if self._messages_load_expects_search:
            return False
        if self._message_list_populating:
            self._pending_sync_folder_refresh = (account_uid, folder_name)
            self._message_sync_in_progress = False
            return False

        if error is not None:
            self._message_sync_in_progress = False
            if account := self._current_account:
                if self._is_viewing_folder(account.uid, folder_name):
                    self._update_message_status(account, folder_name)
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            return False
        if not self._is_viewing_folder(account_uid, folder_name):
            return False

        if not self._search_query:
            if is_post_outbox_folder(folder_name):
                self._sidebar.refresh_outbox_row(account_uid)
            elif not is_heavy_folder_name(folder_name):
                # Heavy folders: don't let refresh overwrite STATUS with summary.
                self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        current = self._current_folder_messages or []
        if message_lists_equivalent_for_ui(
            current,
            messages,
            current_total=self._message_total,
            refreshed_total=total,
        ):
            # Same UID/subject set — still apply seen/flagged changes from the
            # server (OWA Follow Up / read toggles) without a full rebind (#270).
            if self._reconcile_refreshed_message_flags(messages):
                self._update_message_toolbar()
            GLib.idle_add(self._on_messages_sync_finished, load_id, False)
            return False

        self._current_folder_messages = messages
        self._message_total = total
        self._message_list_source = "server"
        self._message_sync_in_progress = False
        self._messages_load_generation += 1
        apply_load_id = self._messages_load_generation
        self._set_status(f"Refreshing {folder_name} From Server…")

        if not messages:
            folder_label = "Outbox" if is_post_outbox_folder(folder_name) else folder_name
            if is_post_outbox_folder(folder_name):
                self._message_empty_label.set_label("No Queued Messages")
            elif self._search_query is not None:
                self._message_empty_label.set_label(
                    self._search_empty_label(folder_label)
                )
            else:
                self._message_empty_label.set_label(f"No Messages in {folder_label}")
            self._message_list_view.clear()
            self._message_stack.set_visible_child_name("empty")
            self._update_message_status(account, folder_name)
            return False

        self._message_stack.set_visible_child_name("list")
        prepended = prepended_message_count(current, messages)
        if prepended > 0:
            self._apply_prepended_folder_messages(
                messages[: min(prepended, MESSAGE_LIST_UI_BIND_CAP)],
                folder_name,
                account=account,
            )
            if self._search_query is None:
                self._try_restore_selected_message(account.uid, folder_name)
            return False

        def after_refresh_list() -> None:
            if self._search_query is None:
                self._try_restore_selected_message(account.uid, folder_name)

        # Must go through the bind-capped path — applying all messages here
        # previously rebound 9k+ Archive rows and OOM-killed the process.
        self._apply_messages_to_list(
            messages,
            folder_name,
            account=account,
            load_id=apply_load_id,
            on_complete=after_refresh_list,
        )
        return False

    def _try_restore_selected_message(self, account_uid: str, folder_name: str) -> None:
        uid = self._pending_restore_message_uid
        if not uid:
            return
        if (
            self._restore_message_folder is not None
            and (account_uid, folder_name) != self._restore_message_folder
        ):
            self._pending_restore_message_uid = None
            return

        self._message_list_view.set_restoring_selection(True)
        if self._message_list_view.select_uid(uid):
            self._pending_restore_message_uid = None
            self._current_message_uid = uid
            self._load_message_body_for_uid(uid, mark_seen=False)
        else:
            self._pending_restore_message_uid = None
            set_active_message_uid(None)
        self._message_list_view.set_restoring_selection(False)

    def _update_message_status(self, account: MailAccount, folder_name: str) -> None:
        shown = self._message_list_view.item_count()
        total = self._message_total
        label = account.display_label
        if is_post_outbox_folder(folder_name):
            if total == 0:
                self._set_status(self._with_load_status_detail(f"No queued messages for {label}"))
            elif total >= 0 and shown < total:
                self._set_status(
                    self._with_load_status_detail(
                        f"Showing {shown} of {total} queued for {label}"
                    )
                )
            elif total >= 0:
                self._set_status(self._with_load_status_detail(f"{total} queued for {label}"))
            else:
                self._set_status(self._with_load_status_detail(f"{shown} queued for {label}"))
            return
        if self._search_query is not None:
            if total == 0:
                self._set_status(
                    self._with_load_status_detail(
                        f"No matches in {label} / {folder_name}"
                    )
                )
            elif total >= 0 and shown < total:
                self._set_status(
                    self._with_load_status_detail(
                        f"Showing {shown} of {total} matches in {label} / {folder_name}"
                    )
                )
            elif total >= 0:
                self._set_status(
                    self._with_load_status_detail(
                        f"{total} matches in {label} / {folder_name}"
                    )
                )
            else:
                self._set_status(
                    self._with_load_status_detail(
                        f"{shown} matches in {label} / {folder_name}"
                    )
                )
            return

        # Trusted STATUS totals (large Archive, or any Trash/Junk lock-in) may
        # be labeled "on server"; Camel summary sizes must not (#208).
        from post.mail.folder_status_cache import status_total_is_trusted

        server_total = -1
        if is_heavy_folder_name(folder_name):
            status = self._mail.get_folder_status_totals(account.uid, folder_name)
            if status is not None:
                server_total = status[1]
        indexed = (
            len(self._current_folder_messages)
            if self._current_folder_messages is not None
            else total
        )

        if is_heavy_folder_name(folder_name) and indexed >= 0:
            trusted = status_total_is_trusted(folder_name, server_total)
            if trusted and server_total > indexed:
                if shown < indexed:
                    self._set_status(
                        self._with_load_status_detail(
                            f"Showing {shown} of {indexed} indexed "
                            f"(server {server_total}) in {label} / {folder_name}"
                        )
                    )
                else:
                    self._set_status(
                        self._with_load_status_detail(
                            f"{indexed} indexed of {server_total} on server "
                            f"in {label} / {folder_name}"
                        )
                    )
                return
            if shown < indexed:
                self._set_status(
                    self._with_load_status_detail(
                        f"Showing {shown} of {indexed} indexed "
                        f"in {label} / {folder_name}"
                    )
                )
            else:
                self._set_status(
                    self._with_load_status_detail(
                        f"{indexed} indexed in {label} / {folder_name}"
                    )
                )
            return

        if total >= 0 and shown < total:
            self._set_status(
                self._with_load_status_detail(
                    f"Showing {shown} of {total} in {label} / {folder_name}"
                )
            )
        elif total >= 0:
            self._set_status(
                self._with_load_status_detail(
                    f"{total} messages in {label} / {folder_name}"
                )
            )
        else:
            self._set_status(
                self._with_load_status_detail(
                    f"{shown} messages in {label} / {folder_name}"
                )
            )

    def _reconcile_refreshed_message_flags(self, refreshed: list[dict]) -> bool:
        """Apply seen/flagged patches when a refresh kept the same message set."""
        current = self._current_folder_messages or []
        # Prefer list-store rows as the baseline so we still call set_message
        # when the folder-cache dict already matches the server but the bound
        # list item was left without notify::message (#289).
        baseline: list[dict] = []
        for message in current:
            list_key = self._message_list_key(message)
            store_message = self._message_list_view.get_message(list_key)
            baseline.append(store_message if store_message is not None else message)
        patches = message_flag_patches(baseline, refreshed)
        if not patches:
            return False
        for list_key, flags in patches:
            self._message_list_view.update_message_flags(list_key, flags)
            self._update_message_flags_in_folder_cache(list_key, flags)
            if (
                list_key == self._current_message_uid
                and self._current_message is not None
            ):
                current_flags = dict(self._current_message.get("flags") or {})
                current_flags.update(flags)
                self._current_message["flags"] = current_flags
                self._reader_pane.update_message_flags(dict(flags))
            location = self._message_location_for_list_key(list_key)
            reader_uid = location[2] if location is not None else list_key
            for window in self._reader_windows:
                window.notify_flags_updated(reader_uid, dict(flags))
        return True

    def _update_message_flags_in_folder_cache(
        self, uid: str, flags: dict
    ) -> None:
        if self._current_folder_messages is None:
            return
        for position, message in enumerate(self._current_folder_messages):
            if self._message_list_key(message) == uid:
                # Replace the row (and flags dict) so we do not mutate a dict
                # still held by MessageListItem without set_message (#289).
                merged = dict(message.get("flags") or {})
                merged.update(flags)
                updated = dict(message)
                updated["flags"] = merged
                self._current_folder_messages[position] = updated
                break

    def _mark_message_read(self, uid: str) -> None:
        flags = self._message_flags_for_uid(uid)
        flags["seen"] = True
        self._message_list_view.update_message_flags(uid, flags)
        self._update_message_flags_in_folder_cache(uid, flags)

    def _uids_for_menu(self, uid: str) -> list[str]:
        selected = self._message_list_view.get_selected_uids()
        if uid in selected:
            return selected
        self._message_list_view.select_uid(uid)
        return [uid]

    def _popup_message_menu(
        self, uid: str, x: float, y: float, popup_widget: Gtk.Widget | None = None
    ) -> None:
        uids = self._uids_for_menu(uid)
        self._context_message_uids = uids

        locations: list[tuple[str, str, str]] = []
        for list_key in uids:
            location = self._message_location_for_list_key(list_key)
            if location is not None:
                locations.append(location)

        can_archive = False
        can_trash = False
        viewing_outbox = False
        if locations:
            non_outbox = [
                (account_uid, folder_name)
                for account_uid, folder_name, _message_uid in locations
                if not is_post_outbox_folder(folder_name)
            ]
            viewing_outbox = not non_outbox
            can_archive = bool(non_outbox) and all(
                bool(
                    self._sidebar.get_move_menu_state(account_uid, folder_name).get(
                        "can_archive"
                    )
                )
                for account_uid, folder_name in non_outbox
            )
            can_trash = all(
                is_post_outbox_folder(folder_name)
                or bool(
                    self._sidebar.get_move_menu_state(account_uid, folder_name).get(
                        "can_trash"
                    )
                )
                for account_uid, folder_name, _message_uid in locations
            )

        self._archive_action.set_enabled(can_archive)
        self._trash_action.set_enabled(can_trash)

        menu = Gio.Menu()
        count = len(uids)
        flags_for_uid = self._message_flags_for_uid
        if viewing_outbox and count == 1:
            queue_id = uids[0]
            list_message = self._message_list_view.get_message(queue_id)
            show_send_now = False
            if list_message is not None:
                send_after = list_message.get("send_after")
                if send_after is not None:
                    show_send_now = float(send_after) > time.time()
            menu.append("Edit", "win.message-outbox-edit")
            menu.append("Move to Drafts", "win.message-outbox-drafts")
            if show_send_now:
                menu.append("Send Now", "win.message-outbox-send-now")
        if not viewing_outbox:
            for action in read_menu_items(
                self._message_seen_states_for_uids(uids, flags_for_uid)
            ):
                menu.append(
                    read_menu_label(action, count),
                    f"win.message-mark-{action}",
                )
            for action in flag_menu_items(
                self._message_flagged_states_for_uids(uids, flags_for_uid)
            ):
                menu.append(
                    flag_menu_label(action, count),
                    f"win.message-{action}",
                )
            if count == 1:
                menu.append("Reply", "win.message-reply")
                menu.append("Reply All", "win.message-reply-all")
                menu.append("Forward", "win.message-forward")
                if self._unsubscribe_action_for_list_key(uids[0]) is not None:
                    menu.append("Unsubscribe…", "win.message-unsubscribe")
                if self._calendar_invite_for_list_key(uids[0]) is not None:
                    menu.append("Add to Calendar…", "win.message-add-to-calendar")
                location = self._message_location_for_list_key(uids[0])
                source_is_sent = False
                if location is not None:
                    account_uid, folder_name, _message_uid = location
                    source_is_sent = self._sidebar.folder_is_sent(
                        account_uid, folder_name
                    )
                if should_offer_send_again(
                    selection_count=count, source_is_sent=source_is_sent
                ):
                    menu.append("Send Again", "win.message-send-again")
        if can_archive:
            menu.append(
                self._count_menu_label("Archive", count), "win.message-archive"
            )
        if can_trash:
            menu.append(
                self._count_menu_label("Move to Trash", count), "win.message-move-trash"
            )
        self._message_popover.set_menu_model(menu)

        parent = self._message_popover.get_parent()
        coords: tuple[float, float] | None = None
        if popup_widget is not None and parent is not None:
            coords = popup_widget.translate_coordinates(parent, x, y)
        if coords is None:
            coords = self._message_list_view.translate_to_scroll(x, y)
        if coords is not None:
            menu_x, menu_y = coords
        else:
            menu_x, menu_y = x, y
        rect = Gdk.Rectangle()
        rect.x = int(menu_x)
        rect.y = int(menu_y)
        rect.width = 1
        rect.height = 1
        self._message_popover.set_pointing_to(rect)
        self._message_popover.popup()

    def _on_message_list_item_pressed(self, uid: str) -> None:
        self._user_message_click_pending = True
        self._pending_restore_message_uid = None
        selected = self._message_list_view.get_selected_uids()
        if len(selected) == 1 and selected[0] == uid:
            if uid == self._current_message_uid and self._reader_shows_list_key(uid):
                return
            self._current_message_uid = uid
            self._load_message_body_for_uid(uid, mark_seen=True)
            return
        self._message_list_view.select_uid(uid)

    def _on_message_list_context_menu(
        self, uid: str, widget: Gtk.Widget, x: float, y: float
    ) -> None:
        self._popup_message_menu(uid, x, y, widget)

    def _on_message_menu_mark_read(self, *_args) -> None:
        self._set_messages_seen(True)

    def _on_message_menu_mark_unread(self, *_args) -> None:
        self._set_messages_seen(False)

    def _on_message_menu_flag(self, *_args) -> None:
        self._set_messages_flagged(True)

    def _on_message_menu_unflag(self, *_args) -> None:
        self._set_messages_flagged(False)

    def _on_message_menu_archive(self, *_args) -> None:
        self._move_context_messages("archive")

    def _on_message_menu_move_trash(self, *_args) -> None:
        self._move_context_messages("trash")

    def _on_message_menu_reply(self, *_args) -> None:
        self._open_compose_on_message("reply")

    def _on_message_menu_reply_all(self, *_args) -> None:
        self._open_compose_on_message("reply-all")

    def _on_message_menu_forward(self, *_args) -> None:
        self._open_compose_on_message("forward")

    def _on_message_menu_unsubscribe(self, *_args) -> None:
        if len(self._context_message_uids) != 1:
            return
        list_key = self._context_message_uids[0]
        action = self._unsubscribe_action_for_list_key(list_key)
        if action is not None:
            self._run_unsubscribe_action(action, list_key=list_key)

    def _on_message_menu_add_to_calendar(self, *_args) -> None:
        if len(self._context_message_uids) != 1:
            return
        invite = self._calendar_invite_for_list_key(self._context_message_uids[0])
        if invite is not None:
            self._on_add_to_calendar_clicked(invite)

    def _on_message_menu_send_again(self, *_args) -> None:
        if len(self._context_message_uids) != 1:
            return
        if not self._mail.list_sendable_accounts():
            self._set_status("No mail account configured for sending")
            return
        list_key = self._context_message_uids[0]
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return
        account_uid, _folder_name, _message_uid = location
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return
        if not account.can_send:
            self._set_status("Selected account has no mail transport configured")
            return
        self._open_send_again(list_key)

    def _on_message_menu_outbox_edit(self, *_args) -> None:
        if len(self._context_message_uids) != 1 or not self._current_account:
            return
        self._open_compose_from_outbox(self._context_message_uids[0])

    def _on_message_menu_outbox_move_drafts(self, *_args) -> None:
        if len(self._context_message_uids) != 1 or not self._current_account:
            return
        self._move_outbox_to_drafts(self._context_message_uids[0])

    def _on_message_menu_outbox_send_now(self, *_args) -> None:
        if len(self._context_message_uids) != 1:
            return
        queue_id = self._context_message_uids[0]
        self._send_delay_scheduler.send_now(queue_id)
        self._set_status("Sending message…")

    def _open_compose_from_outbox(self, queue_id: str) -> None:
        if not self._current_account:
            return
        self._send_delay_scheduler.cancel(queue_id)
        self._present_compose_window(
            self._current_account,
            mode="outbox",
            outbox_queue_id=queue_id,
        )

    def _move_outbox_to_drafts(self, queue_id: str) -> None:
        self._send_delay_scheduler.cancel(queue_id)

        def worker() -> None:
            error: Exception | None = None
            try:
                queued = load_queued_outbound_message(queue_id)
                attachments = load_queued_attachments(queue_id, queued)
                self._mail.save_draft(
                    queued.account_uid,
                    to=queued.to,
                    cc=queued.cc,
                    bcc=queued.bcc,
                    subject=queued.subject,
                    body=queued.body,
                    body_html=queued.body_html,
                    in_reply_to=queued.in_reply_to,
                    references=queued.references,
                    attachments=attachments or None,
                )
                remove_queued_outbound_message(queue_id)
            except Exception as exc:
                log.exception("Failed to move outbox message to drafts")
                error = exc
            GLib.idle_add(self._on_outbox_moved_to_drafts, error)

        get_mail_io_thread().submit(worker)

    def _on_outbox_moved_to_drafts(self, error: Exception | None) -> bool:
        if error is not None:
            show_error_toast(self, f"Could not move to Drafts: {error}")
            return False
        self._on_outbox_changed()
        self._set_status("Moved queued message to Drafts")
        return False

    def _on_status_send_now_clicked(self, *_args) -> None:
        if self._stop_sending_in_flight:
            return
        pending = [
            queue_id
            for queue_id, message in list_queued_outbound_messages()
            if has_pending_send_delay(message)
        ]
        if not pending:
            return
        for queue_id in pending:
            self._send_delay_scheduler.send_now(queue_id)
        if len(pending) == 1:
            self._set_status("Sending message…")
        else:
            self._set_status(f"Sending {len(pending)} messages…")

    def _on_stop_sending_clicked(self, *_args) -> None:
        if self._stop_sending_in_flight:
            return
        pending = [
            (queue_id, message)
            for queue_id, message in list_queued_outbound_messages()
            if has_pending_send_delay(message)
        ]
        if not pending:
            return
        self._stop_sending_in_flight = True
        self._status_send_now_btn.set_sensitive(False)
        self._stop_sending_btn.set_sensitive(False)
        for queue_id, _message in pending:
            self._send_delay_scheduler.cancel(queue_id)
        queue_ids = [queue_id for queue_id, _message in pending]

        def worker() -> None:
            moved: dict[str, int] = {}
            failed: dict[str, int] = {}
            for queue_id in queue_ids:
                account_uid = ""
                try:
                    queued = try_load_queued_outbound_message(queue_id)
                    if queued is None:
                        # Already moved/removed by an earlier stop or move.
                        continue
                    account_uid = queued.account_uid
                    attachments = load_queued_attachments(queue_id, queued)
                    self._mail.save_draft(
                        queued.account_uid,
                        to=queued.to,
                        cc=queued.cc,
                        bcc=queued.bcc,
                        subject=queued.subject,
                        body=queued.body,
                        body_html=queued.body_html,
                        in_reply_to=queued.in_reply_to,
                        references=queued.references,
                        attachments=attachments or None,
                    )
                    remove_queued_outbound_message(queue_id)
                    moved[account_uid] = moved.get(account_uid, 0) + 1
                except Exception:
                    log.exception(
                        "Failed to move delayed outbox message %s to drafts",
                        queue_id,
                    )
                    key = account_uid or queue_id
                    failed[key] = failed.get(key, 0) + 1
            GLib.idle_add(self._on_stop_sending_finished, moved, failed)

        get_mail_io_thread().submit(worker)

    def _on_stop_sending_finished(
        self,
        moved: dict[str, int],
        failed: dict[str, int],
    ) -> bool:
        self._stop_sending_in_flight = False
        self._on_outbox_changed()
        if moved:
            moved_labels = [
                (self._sidebar.account_display_label(account_uid), count)
                for account_uid, count in moved.items()
            ]
            show_toast(self, format_stop_sending_toast(moved_labels))
        if failed:
            failed_labels = [
                (self._sidebar.account_display_label(account_uid), count)
                for account_uid, count in failed.items()
            ]
            show_error_toast(self, format_stop_sending_error_toast(failed_labels))
        return False

    def _move_selected_messages(self, destination: str) -> None:
        uids = self._message_list_view.get_selected_uids()
        if not uids:
            return
        self._move_messages(destination, uids)

    def _move_context_messages(self, destination: str) -> None:
        self._move_messages(destination, list(self._context_message_uids))

    def _delete_queued_messages(self, queue_ids: list[str]) -> None:
        if not self._current_account:
            return
        for queue_id in queue_ids:
            self._send_delay_scheduler.cancel(queue_id)
            remove_queued_outbound_message(queue_id)
        count = len(queue_ids)
        if count == 1:
            self._set_status("Removed 1 queued message")
        else:
            self._set_status(f"Removed {count} queued messages")
        self._on_outbox_changed()

    def _move_messages(
        self,
        destination: str,
        uids: list[str],
        *,
        account_uid: str | None = None,
        folder_name: str | None = None,
    ) -> None:
        if not uids:
            return

        # Explicit location (reader window): *uids* are Camel message UIDs.
        if account_uid is not None and folder_name is not None:
            groups: dict[tuple[str, str], list[tuple[str, str]]] = {
                (account_uid, folder_name): [(uid, uid) for uid in uids]
            }
        else:
            # List/context selection: *uids* are list keys (plain or search).
            groups = group_list_keys_by_location(
                list(uids), self._message_location_for_list_key
            )
        if not groups:
            return

        self._message_popover.popdown()
        self._clear_move_undo()

        label = "Trash" if destination == "trash" else "Archive"
        total_messages = sum(len(pairs) for pairs in groups.values())
        self._set_status(f"Moving {total_messages} message(s) to {label}…")

        for (group_account, group_folder), pairs in groups.items():
            list_keys = [list_key for list_key, _message_uid in pairs]
            message_uids = [message_uid for _list_key, message_uid in pairs]

            state = self._sidebar.get_move_menu_state(group_account, group_folder)
            if destination == "archive" and not state.get("can_archive"):
                continue
            if destination == "trash" and not (
                state.get("can_trash") or is_post_outbox_folder(group_folder)
            ):
                continue

            if is_post_outbox_folder(group_folder) and destination == "trash":
                self._delete_queued_messages(message_uids)
                continue

            if self._mail.get_account_transfer_state(group_account) != "idle":
                show_error_toast(
                    self,
                    "A previous move is still in progress or the server is not "
                    "responding; try again in a moment",
                )
                continue

            self._suppress_sync_list_reload = (group_account, group_folder)

            # Optimistic UI: remove rows immediately so Archive doesn't look
            # stuck while Camel transfer_messages_to_sync blocks (M365).
            self._message_list_view.remove_uids(list_keys)
            cleared_current = any(
                key == self._current_message_uid for key in list_keys
            )
            if cleared_current:
                self._clear_reader()
                set_active_message_uid(None)
                self._restore_message_folder = None

            def worker(
                *,
                account_uid: str = group_account,
                folder_name: str = group_folder,
                camel_uids: list[str] = list(message_uids),
                ui_keys: list[str] = list(list_keys),
            ) -> None:
                error: Exception | None = None
                result: dict | None = None
                try:
                    if destination == "trash":
                        result = self._mail.move_messages_to_trash(
                            account_uid, folder_name, camel_uids
                        )
                    else:
                        result = self._mail.archive_messages(
                            account_uid, folder_name, camel_uids
                        )
                except Exception as exc:
                    log.exception("Failed to move messages to %s", destination)
                    error = exc
                finally:
                    self._mail.end_folder_transfer()
                GLib.idle_add(
                    self._on_messages_moved,
                    account_uid,
                    folder_name,
                    ui_keys,
                    destination,
                    result,
                    error,
                )

            self._mail.begin_folder_transfer()
            get_mail_io_thread().submit(worker)

    def _on_messages_dropped(
        self,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        uids: list[str],
    ) -> None:
        self._move_messages_to_folder(
            account_uid, source_folder, dest_folder, uids
        )

    def _move_messages_to_folder(
        self,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        uids: list[str],
    ) -> None:
        if not uids or source_folder == dest_folder:
            return

        if self._mail.get_account_transfer_state(account_uid) != "idle":
            show_error_toast(
                self,
                "A previous move is still in progress or the server is not "
                "responding; try again in a moment",
            )
            return

        uids = list(uids)
        self._clear_move_undo()
        self._suppress_sync_list_reload = (account_uid, source_folder)

        def worker() -> None:
            error: Exception | None = None
            result: dict | None = None
            try:
                result = self._mail.move_messages(
                    account_uid, source_folder, dest_folder, uids
                )
            except Exception as exc:
                log.exception(
                    "Failed to move messages to folder %r", dest_folder
                )
                error = exc
            finally:
                self._mail.end_folder_transfer()
            GLib.idle_add(
                self._on_folder_messages_moved,
                account_uid,
                source_folder,
                dest_folder,
                uids,
                result,
                error,
            )

        display = self._sidebar.folder_display_name(account_uid, dest_folder)
        if len(uids) == 1:
            self._set_status(f"Moving message to {display}…")
        else:
            self._set_status(f"Moving {len(uids)} messages to {display}…")
        self._mail.begin_folder_transfer()
        get_mail_io_thread().submit(worker)

    def _on_folder_messages_moved(
        self,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        uids: list[str],
        result: dict | None,
        error: Exception | None,
    ) -> bool:
        moved_count = len(uids)
        display = self._sidebar.folder_display_name(account_uid, dest_folder)
        if moved_count > 1:
            status_label = f"Moved {moved_count} messages to {display}"
        else:
            status_label = f"Moved message to {display}"
        return self._on_messages_moved(
            account_uid,
            source_folder,
            uids,
            dest_folder,
            result,
            error,
            status_label=status_label,
        )

    def _dismiss_undo_toast_only(self) -> None:
        if self._undo_toast is not None:
            self._undo_toast.dismiss()
            self._undo_toast = None

    def _clear_move_undo(self) -> None:
        self._pending_move_undo = None
        self._undo_move_action.set_enabled(False)
        self._dismiss_undo_toast_only()

    def _trigger_move_undo(self) -> bool:
        undo = self._pending_move_undo
        if undo is None:
            return False
        self._clear_move_undo()
        self._undo_message_move(undo)
        return True

    def _arm_move_undo(
        self,
        *,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        dest_uids: list[str],
    ) -> bool:
        if not dest_uids:
            log.warning("Move succeeded but destination UIDs are unknown; undo disabled")
            return False
        self._pending_move_undo = {
            "account_uid": account_uid,
            "source_folder": source_folder,
            "dest_folder": dest_folder,
            "dest_uids": dest_uids,
        }
        self._undo_move_action.set_enabled(True)
        return True

    def _show_move_undo_toast(self, label: str) -> bool:
        if self._pending_move_undo is None:
            return False
        toast = Adw.Toast.new(label)
        toast.set_button_label("Undo")
        toast.set_action_name("win.undo-move")
        toast.set_priority(Adw.ToastPriority.HIGH)
        toast.set_timeout(10)
        toast.connect("dismissed", self._on_move_undo_dismissed)
        self._undo_toast = toast
        self._toast_overlay.add_toast(toast)
        return False

    def _show_bulk_archive_done_toast(self, status_label: str) -> bool:
        show_toast(
            self,
            status_label,
            priority=Adw.ToastPriority.HIGH,
            timeout=10,
        )
        return False

    def _register_move_undo(
        self,
        label: str,
        *,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        dest_uids: list[str],
    ) -> None:
        if not self._arm_move_undo(
            account_uid=account_uid,
            source_folder=source_folder,
            dest_folder=dest_folder,
            dest_uids=dest_uids,
        ):
            return
        self._show_move_undo_toast(label)

    def _on_move_undo_dismissed(self, _toast: Adw.Toast) -> None:
        self._undo_toast = None

    def _undo_message_move(self, undo: dict) -> None:
        def worker() -> None:
            error: Exception | None = None
            result: dict | None = None
            try:
                result = self._mail.move_messages(
                    undo["account_uid"],
                    undo["dest_folder"],
                    undo["source_folder"],
                    undo["dest_uids"],
                )
            except Exception as exc:
                log.exception("Failed to undo message move")
                error = exc
            finally:
                self._mail.end_folder_transfer()
            GLib.idle_add(self._on_move_undo_finished, undo, result, error)

        self._set_status("Restoring messages…")
        self._mail.begin_folder_transfer()
        get_mail_io_thread().submit(worker)

    def _on_move_undo_finished(
        self,
        undo: dict,
        result: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Undo failed: {error}")
            return False
        if result is None:
            return False

        account_uid = undo["account_uid"]
        source_folder = undo["source_folder"]
        dest_folder = undo["dest_folder"]

        if self._current_account and self._current_account.uid == account_uid:
            source_unread = result.get("source_folder_unread")
            source_total = result.get("source_folder_total")
            if source_unread is not None and source_total is not None:
                self._sidebar.update_folder_row(
                    account_uid, dest_folder, source_unread, source_total
                )

            dest_unread = result.get("destination_folder_unread")
            dest_total = result.get("destination_folder_total")
            if dest_unread is not None and dest_total is not None:
                self._sidebar.update_folder_row(
                    account_uid, source_folder, dest_unread, dest_total
                )

            inbox_folder = self._sidebar.inbox_folder_for_account(account_uid)
            if inbox_folder in (source_folder, dest_folder):
                self._sidebar.refresh_inbox_counts(account_uid)

            if self._current_folder in (source_folder, dest_folder):
                self._load_messages(account_uid, self._current_folder)
            elif self._current_account and self._current_folder:
                self._update_message_status(
                    self._current_account, self._current_folder
                )

        self._set_status("Move undone")
        return False

    def _update_sidebar_from_move_result(
        self, account_uid: str, result: dict
    ) -> None:
        source_folder = result.get("source_folder")
        if source_folder:
            unread = result.get("source_folder_unread")
            total = result.get("source_folder_total")
            if unread is not None and total is not None:
                self._sidebar.update_folder_row(
                    account_uid,
                    source_folder,
                    unread,
                    total,
                )

        dest_folder = result.get("destination_folder")
        dest_unread = result.get("destination_folder_unread")
        dest_total = result.get("destination_folder_total")
        if (
            dest_folder
            and dest_unread is not None
            and dest_total is not None
        ):
            self._sidebar.update_folder_row(
                account_uid,
                dest_folder,
                dest_unread,
                dest_total,
            )

        if (
            self._current_account
            and self._current_account.uid == account_uid
            and self._current_folder == source_folder
        ):
            self._update_message_status(self._current_account, self._current_folder)

            inbox_folder = self._sidebar.inbox_folder_for_account(account_uid)
            if inbox_folder and self._current_folder == inbox_folder:
                self._sidebar.refresh_inbox_counts(account_uid)

    def _move_status_label(self, destination: str, moved_count: int) -> str:
        label = "Trash" if destination == "trash" else "Archive"
        if moved_count > 1:
            return f"Moved {moved_count} messages to {label}"
        return f"Moved message to {label}"

    def _finalize_move_status_and_undo(
        self,
        account_uid: str,
        source_folder: str,
        destination: str,
        uids: list[str],
        result: dict,
        *,
        status_label: str | None = None,
    ) -> None:
        moved_count = len(result.get("moved_uids") or [])
        if status_label is None:
            status_label = self._move_status_label(destination, moved_count)

        if result.get("queued"):
            self._clear_move_undo()
            if moved_count == 1:
                self._set_status("Queued 1 message — will sync when online")
            else:
                self._set_status(
                    f"Queued {moved_count} messages — will sync when online"
                )
            self._refresh_status_display()
            return

        dest_folder = result.get("destination_folder")
        dest_uids = result.get("destination_uids") or []
        if dest_folder and dest_uids:
            self._register_move_undo(
                status_label,
                account_uid=account_uid,
                source_folder=source_folder,
                dest_folder=dest_folder,
                dest_uids=dest_uids,
            )
            self._set_status(f"{status_label}  ·  Ctrl+Z to undo")
        else:
            self._clear_move_undo()
            self._set_status(status_label)

    def _on_messages_moved(
        self,
        account_uid: str,
        folder_name: str,
        uids: list[str],
        destination: str,
        result: dict | None,
        error: Exception | None,
        *,
        status_label: str | None = None,
    ) -> bool:
        suppress_key = (account_uid, folder_name)

        if error is not None:
            if self._suppress_sync_list_reload == suppress_key:
                self._suppress_sync_list_reload = None
            show_error_toast(self, f"Could not move messages: {error}")
            # Restore optimistic list removals.
            if (
                self._current_account
                and self._current_folder
                and self._current_account.uid == account_uid
                and self._current_folder == folder_name
            ):
                self._load_messages(account_uid, folder_name, sync=False)
            return False
        if result is None:
            if self._suppress_sync_list_reload == suppress_key:
                self._suppress_sync_list_reload = None
            return False

        removed_count = self._message_list_view.remove_uids(uids)
        cleared_current = any(uid == self._current_message_uid for uid in uids)

        if cleared_current:
            self._clear_reader()
            set_active_message_uid(None)
            self._restore_message_folder = None

        moved_count = len(result.get("moved_uids") or [])
        # Optimistic remove already dropped rows; still count them for totals.
        count_delta = removed_count if removed_count > 0 else moved_count
        if self._message_total >= 0:
            self._message_total = max(0, self._message_total - count_delta)

        moved_keys = set(uids)
        if self._current_folder_messages is not None:
            self._current_folder_messages = [
                message
                for message in self._current_folder_messages
                if self._message_list_key(message) not in moved_keys
            ]

        # Do not _load_messages on success: disk cache can still contain the
        # moved UIDs and a reload resurrects them (Archive Trash looked like a
        # no-op). Rows are already removed above / via optimistic UI.
        if self._message_list_view.item_count() == 0 and folder_name:
            self._message_empty_label.set_label(
                f"No Messages in {folder_name}"
            )
            self._message_stack.set_visible_child_name("empty")

        self._update_sidebar_from_move_result(account_uid, result)
        self._finalize_move_status_and_undo(
            account_uid,
            folder_name,
            destination,
            uids,
            result,
            status_label=status_label,
        )
        self._update_message_toolbar()
        self._notify_reader_windows_message_moved(uids)

        if self._suppress_sync_list_reload == suppress_key:
            self._suppress_sync_list_reload = None
        return False

    def _set_messages_seen(self, seen: bool) -> None:
        self._set_message_flags("seen", seen=seen)

    def _set_messages_flagged(self, flagged: bool) -> None:
        self._set_message_flags("flagged", flagged=flagged)

    def _set_message_flags(
        self,
        flag_name: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
        uids: list[str] | None = None,
    ) -> None:
        list_keys = list(uids if uids is not None else self._context_message_uids)
        if not list_keys:
            return

        groups = group_list_keys_by_location(
            list_keys, self._message_location_for_list_key
        )
        if not groups:
            return

        updates_by_list_key: dict[str, dict] = {}
        folder_count_updates: list[tuple[str, str, int, int]] = []
        any_queued = False
        error: Exception | None = None

        for (account_uid, folder_name), pairs in groups.items():
            message_uids = [message_uid for _list_key, message_uid in pairs]
            list_key_by_uid = {
                message_uid: list_key for list_key, message_uid in pairs
            }
            try:
                if flag_name == "seen":
                    assert seen is not None
                    result = self._mail.set_messages_seen(
                        account_uid, folder_name, message_uids, seen=seen
                    )
                else:
                    assert flagged is not None
                    result = self._mail.set_messages_flagged(
                        account_uid, folder_name, message_uids, flagged=flagged
                    )
            except Exception as exc:
                log.exception("Failed to update message %s", flag_name)
                error = exc
                break

            for item in result.get("updates") or []:
                camel_uid = item.get("uid")
                list_key = list_key_by_uid.get(str(camel_uid)) if camel_uid else None
                if list_key is None:
                    continue
                updates_by_list_key[list_key] = dict(item.get("flags") or {})
            if result.get("queued"):
                any_queued = True
            if flag_name == "seen":
                unread = result.get("folder_unread")
                total = result.get("folder_total")
                if unread is not None and total is not None:
                    folder_count_updates.append(
                        (account_uid, folder_name, int(unread), int(total))
                    )

        self._apply_message_flag_updates(
            updates_by_list_key,
            flag_name,
            folder_count_updates=folder_count_updates,
            queued=any_queued,
            error=error,
        )

    def _apply_message_flag_updates(
        self,
        updates_by_list_key: dict[str, dict],
        flag_name: str,
        *,
        folder_count_updates: list[tuple[str, str, int, int]],
        queued: bool,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Could not update messages: {error}")
            return
        if not updates_by_list_key and not folder_count_updates:
            return

        for list_key, flags in updates_by_list_key.items():
            self._message_list_view.update_message_flags(list_key, flags)
            self._update_message_flags_in_folder_cache(list_key, flags)
            if list_key == self._current_message_uid and self._current_message is not None:
                current_flags = dict(self._current_message.get("flags") or {})
                current_flags.update(flags)
                self._current_message["flags"] = current_flags
                self._reader_pane.update_message_flags(dict(flags))

            location = self._message_location_for_list_key(list_key)
            reader_uid = location[2] if location is not None else list_key
            for window in self._reader_windows:
                window.notify_flags_updated(reader_uid, dict(flags))

        for account_uid, folder_name, unread, total in folder_count_updates:
            self._suppress_sync_list_reload = (account_uid, folder_name)
            self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        count = len(updates_by_list_key)
        if queued:
            if count == 1:
                self._set_status("Queued 1 action — will sync when online")
            elif count > 1:
                self._set_status(f"Queued {count} actions — will sync when online")
            self._refresh_status_display()
            self._update_message_toolbar()
            return
        if count > 1:
            self._set_status(f"Updated {count} messages")
        self._update_message_toolbar()

    def _on_message_list_selection_changed(self) -> None:
        GLib.idle_add(self._on_message_list_selection_changed_idle)

    def _on_message_list_selection_changed_idle(self) -> bool:
        self._update_message_toolbar()
        if self._message_list_view.is_restoring_selection():
            return False
        if not self._current_account or not self._current_folder:
            return False

        selected = self._message_list_view.get_selected_uids()
        if len(selected) != 1:
            self._user_message_click_pending = False
            return False

        uid = selected[0]
        if uid == self._current_message_uid and self._reader_shows_list_key(uid):
            return False

        mark_seen = self._user_message_click_pending
        if mark_seen:
            self._pending_restore_message_uid = None
        self._user_message_click_pending = False
        self._current_message_uid = uid
        self._load_message_body_for_uid(uid, mark_seen=mark_seen)
        return False

    def _on_message_list_item_activated(self, uid: str) -> None:
        if self._message_list_view.is_restoring_selection():
            return
        if len(self._message_list_view.get_selected_uids()) != 1:
            return
        location = self._message_location_for_list_key(uid)
        if location is None:
            return
        account_uid, folder_name, message_uid = location
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return
        action = message_list_activate_action(
            is_drafts_folder=self._sidebar.folder_is_drafts(account.uid, folder_name),
            is_outbox_folder=is_post_outbox_folder(folder_name),
        )
        if action == MessageListActivateAction.DRAFT_COMPOSE:
            self._open_draft_for_editing(message_uid, account=account, folder_name=folder_name)
            return
        if action == MessageListActivateAction.OUTBOX_EDIT:
            self._open_compose_from_outbox(message_uid)
            return
        self._present_reader_window(uid)

    def _update_message_toolbar(self) -> bool:
        selected = self._message_list_view.get_selected_uids()
        can_use_reader_actions = (
            len(selected) == 1
            and self._current_message_uid is not None
            and selected[0] == self._current_message_uid
        )
        self._set_message_actions_sensitive(can_use_reader_actions)

        can_archive = False
        can_trash = False
        can_toggle_flags = False
        if selected:
            locations = [
                location
                for list_key in selected
                if (location := self._message_location_for_list_key(list_key))
                is not None
            ]
            if locations:
                non_outbox = [
                    (account_uid, folder_name)
                    for account_uid, folder_name, _message_uid in locations
                    if not is_post_outbox_folder(folder_name)
                ]
                can_archive = bool(non_outbox) and all(
                    bool(
                        self._sidebar.get_move_menu_state(
                            account_uid, folder_name
                        ).get("can_archive")
                    )
                    for account_uid, folder_name in non_outbox
                )
                can_trash = all(
                    is_post_outbox_folder(folder_name)
                    or bool(
                        self._sidebar.get_move_menu_state(
                            account_uid, folder_name
                        ).get("can_trash")
                    )
                    for account_uid, folder_name, _message_uid in locations
                )
                can_toggle_flags = bool(non_outbox) and len(non_outbox) == len(
                    locations
                )

        seen = None
        flagged = None
        if can_toggle_flags:
            flags_for_uid = self._message_flags_for_uid
            seen = uniform_bool_state(
                self._message_seen_states_for_uids(selected, flags_for_uid)
            )
            flagged = uniform_bool_state(
                self._message_flagged_states_for_uids(selected, flags_for_uid)
            )
            if seen is not None or flagged is not None:
                toggles = reader_toggle_button_state(
                    {
                        "seen": True if seen is None else seen,
                        "flagged": False if flagged is None else flagged,
                    }
                )
                if seen is not None:
                    self._apply_toggle_button_presentation(
                        self._header_read_btn, toggles["read"]
                    )
                if flagged is not None:
                    self._apply_toggle_button_presentation(
                        self._header_flag_btn, toggles["flag"]
                    )

        self._header_read_btn.set_sensitive(seen is not None)
        self._header_flag_btn.set_sensitive(flagged is not None)
        self._header_archive_btn.set_sensitive(can_archive)
        self._header_trash_btn.set_sensitive(can_trash)
        return False

    def _load_message_body_for_uid(
        self,
        uid: str,
        *,
        mark_seen: bool,
    ) -> None:
        location = self._message_location_for_list_key(uid)
        if location is None:
            return
        account_uid, folder_name, message_uid = location
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return

        self._message_read_generation += 1
        read_id = self._message_read_generation
        self._pending_message_read_uid = uid
        self._inflight_message_read_id = read_id
        self._current_message = None
        self._reader_pane.show_loading()
        viewing_outbox = is_post_outbox_folder(folder_name)
        viewing_drafts = self._sidebar.folder_is_drafts(account.uid, folder_name)
        from_label = account.email or account.display_label

        def worker() -> None:
            if read_id != self._message_read_generation:
                GLib.idle_add(
                    self._on_message_read_worker_stale,
                    read_id,
                    uid,
                )
                return
            error: Exception | None = None
            msg: dict | None = None
            try:
                if viewing_outbox:
                    msg = read_queued_message(
                        message_uid,
                        account_uid=account.uid,
                        from_label=from_label,
                    )
                else:
                    msg = self._mail.read_message(
                        account.uid,
                        folder_name,
                        message_uid,
                        mark_seen=mark_seen and not viewing_drafts,
                    )
            except MessageNotAvailableError as exc:
                log.warning(
                    "Message %s no longer available in %r",
                    uid,
                    folder_name,
                )
                error = exc
            except Exception as exc:
                log_mail_error(log, "Failed to read message", exc)
                error = exc
            GLib.idle_add(
                self._on_message_read,
                read_id,
                uid,
                msg,
                error,
            )

        get_mail_io_thread().submit_front(worker)

    def _open_draft_for_editing(
        self,
        uid: str,
        *,
        account: MailAccount | None = None,
        folder_name: str | None = None,
    ) -> None:
        if account is None:
            if not self._current_account:
                return
            account = self._current_account
        if folder_name is None:
            if not self._current_folder:
                return
            folder_name = self._current_folder

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                msg = self._mail.read_message(
                    account.uid,
                    folder_name,
                    uid,
                    mark_seen=False,
                )
            except MessageNotAvailableError as exc:
                log.warning(
                    "Draft %s no longer available in %r",
                    uid,
                    folder_name,
                )
                error = exc
            except Exception as exc:
                log.exception("Failed to read draft for editing")
                error = exc
            GLib.idle_add(
                self._on_draft_compose_loaded,
                account,
                folder_name,
                uid,
                msg,
                error,
            )

        get_mail_io_thread().submit(worker)

    def _on_draft_compose_loaded(
        self,
        account: MailAccount,
        folder_name: str,
        message_uid: str,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Could not open draft: {error}")
            return False
        if msg is None:
            return False
        self._present_compose_window(
            account,
            mode="draft",
            draft_folder_name=folder_name,
            draft_message_uid=message_uid,
            draft_message=msg,
        )
        return False

    def _open_send_again(self, list_key: str) -> None:
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return
        account_uid, folder_name, message_uid = location
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                msg = self._mail.read_message(
                    account.uid,
                    folder_name,
                    message_uid,
                    mark_seen=False,
                )
            except MessageNotAvailableError as exc:
                log.warning(
                    "Sent message %s no longer available in %r",
                    message_uid,
                    folder_name,
                )
                error = exc
            except Exception as exc:
                log.exception("Failed to read sent message for send again")
                error = exc
            GLib.idle_add(
                self._on_send_again_compose_loaded,
                account,
                folder_name,
                message_uid,
                msg,
                error,
            )

        get_mail_io_thread().submit(worker)

    def _on_send_again_compose_loaded(
        self,
        account: MailAccount,
        folder_name: str,
        message_uid: str,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Could not open message: {error}")
            return False
        if msg is None:
            return False
        self._present_compose_window(
            account,
            mode="send-again",
            draft_folder_name=folder_name,
            draft_message_uid=message_uid,
            draft_message=msg,
        )
        return False

    def _mark_seen_when_reading_uid(self, uid: str) -> bool:
        location = self._message_location_for_list_key(uid)
        if location is None:
            return True
        account_uid, folder_name, _message_uid = location
        return not self._sidebar.folder_is_drafts(account_uid, folder_name)

    def _recover_stale_message_read(self, read_id: int, uid: str) -> bool:
        if read_id == self._message_read_generation:
            return False
        if self._current_message_uid != uid or self._current_message is not None:
            return False
        if (
            self._inflight_message_read_id is not None
            and self._inflight_message_read_id != read_id
        ):
            return False
        self._load_message_body_for_uid(
            uid,
            mark_seen=self._mark_seen_when_reading_uid(uid),
        )
        return False

    def _on_message_read_worker_stale(self, read_id: int, uid: str) -> bool:
        return self._recover_stale_message_read(read_id, uid)

    def _on_message_read(
        self,
        read_id: int,
        uid: str,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if read_id != self._message_read_generation:
            return self._recover_stale_message_read(read_id, uid)

        self._pending_message_read_uid = None
        self._inflight_message_read_id = None

        if isinstance(error, MessageNotAvailableError):
            if error.reason == MessageUnavailableReason.VANISHED:
                self._remove_vanished_message(uid)
            sign_in_required = (
                error.reason == MessageUnavailableReason.NOT_CACHED_SIGN_IN
            )
            if sign_in_required:
                location = self._message_location_for_list_key(uid)
                account_uid = (
                    location[0]
                    if location is not None
                    else (
                        self._current_account.uid
                        if self._current_account is not None
                        else None
                    )
                )
                if account_uid is not None:
                    self._mail.set_account_connect_health(
                        account_uid, "needs_sign_in"
                    )
            self._show_message_unavailable_reader(error.user_message())
            if not sign_in_required:
                show_error_toast(self, error.user_message())
            return False

        if error is not None:
            self._pending_message_read_uid = None
            location = self._message_location_for_list_key(uid)
            account_uid = (
                location[0]
                if location is not None
                else (
                    self._current_account.uid
                    if self._current_account is not None
                    else None
                )
            )
            sign_in_required = is_sign_in_required_error(error) or (
                account_uid is not None
                and self._mail.get_account_connect_health(account_uid)
                == "needs_sign_in"
            )
            if account_uid is not None and sign_in_required:
                self._mail.set_account_connect_health(account_uid, "needs_sign_in")
            user_message = format_message_read_error(
                error,
                cached=False,
            )
            self._show_message_unavailable_reader(user_message)
            if not sign_in_required:
                show_error_toast(self, user_message)
            return False

        assert msg is not None

        previous_uid = str(msg.get("_previous_uid") or "")
        recovered_uid = str(msg.get("uid") or "")
        if previous_uid and recovered_uid and previous_uid != recovered_uid:
            self._remap_folder_message_uids({previous_uid: recovered_uid})
            uid = self._remap_list_key(uid, {previous_uid: recovered_uid}) or uid

        self._current_message_uid = uid
        set_active_message_uid(uid)
        location = self._message_location_for_list_key(uid)
        if location is not None:
            self._restore_message_folder = location[:2]
        elif self._current_account and self._current_folder:
            self._restore_message_folder = (
                self._current_account.uid,
                self._current_folder,
            )
        if location is not None and "folder_unread" in msg and "folder_total" in msg:
            src_account_uid, src_folder_name, _message_uid = location
            self._sidebar.update_folder_row(
                src_account_uid,
                src_folder_name,
                msg["folder_unread"],
                msg["folder_total"],
            )
        elif (
            self._current_account is not None
            and self._current_folder is not None
            and "folder_unread" in msg
            and "folder_total" in msg
        ):
            self._sidebar.update_folder_row(
                self._current_account.uid,
                self._current_folder,
                msg["folder_unread"],
                msg["folder_total"],
            )
        self._current_message = msg
        body = {
            "plain": msg.get("body_plain"),
            "html": msg.get("body_html"),
        }
        self._reader_pane.show_message(
            msg,
            body=body,
            allow_remote=self._load_remote_content,
            dark=self._app_prefers_dark(),
            message_appearance=self._message_appearance,
        )
        if (msg.get("flags") or {}).get("seen"):
            self._mark_message_read(uid)
        return False

    def _app_prefers_dark(self) -> bool:
        return Adw.StyleManager.get_default().get_dark()

    def _on_app_dark_changed(self, *_args) -> None:
        self._reader_pane.refresh_document(dark=self._app_prefers_dark())

    def _present_reader_window(self, list_key: str) -> None:
        location = self._message_location_for_list_key(list_key)
        if location is None:
            return
        account_uid, folder_name, message_uid = location
        for window in self._reader_windows:
            if (
                window.account_uid == account_uid
                and window.folder_name == folder_name
                and window.message_uid == message_uid
            ):
                window.present()
                return
        try:
            account = self._mail.get_account(account_uid)
        except ValueError:
            return
        viewing_drafts = self._sidebar.folder_is_drafts(account.uid, folder_name)

        window = ReaderWindow(
            parent=self,
            mail=self._mail,
            account=account,
            folder_name=folder_name,
            message_uid=message_uid,
            set_status=self._set_status,
            on_compose=self._on_reader_window_compose,
            on_new_message_to=self._on_new_message_to_address,
            on_search_messages_from=self._search_messages_from_address,
            can_search_messages=lambda: self._header_search_entry.get_sensitive(),
            on_request_move=self._on_reader_window_request_move,
            on_flags_updated=self._on_reader_window_flags_updated,
            on_message_loaded=self._on_reader_window_message_loaded,
            get_move_state=lambda: self._sidebar.get_move_menu_state(
                account.uid, folder_name
            ),
            get_message_flags=self._message_flags_for_uid,
            viewing_drafts=viewing_drafts,
        )
        self._reader_windows.append(window)
        window.connect(
            "destroy",
            lambda *_args, w=window: self._reader_windows.remove(w)
            if w in self._reader_windows
            else None,
        )
        window.present()

    def _on_reader_window_compose(
        self,
        mode: str,
        msg: dict,
        account: MailAccount,
        folder_name: str,
        message_uid: str,
    ) -> None:
        self._present_compose_window(
            account,
            mode=mode,
            reply_to=msg,
            source_folder_name=folder_name,
            source_message_uid=message_uid,
        )

    def _on_reader_window_request_move(
        self,
        destination: str,
        message_uid: str,
        account_uid: str,
        folder_name: str,
    ) -> None:
        self._move_messages(
            destination,
            [message_uid],
            account_uid=account_uid,
            folder_name=folder_name,
        )

    def _on_reader_window_flags_updated(self, uid: str, flags: dict) -> None:
        # Reader windows report Camel message UIDs; map back to the list key
        # used by the message list (plain uid or search row key).
        list_key = uid
        if self._current_folder_messages:
            for message in self._current_folder_messages:
                if str(message.get("uid") or "") == uid:
                    list_key = self._message_list_key(message)
                    break
        self._message_list_view.update_message_flags(list_key, flags)
        self._update_message_flags_in_folder_cache(list_key, flags)
        if list_key == self._current_message_uid and self._current_message is not None:
            current_flags = dict(self._current_message.get("flags") or {})
            current_flags.update(flags)
            self._current_message["flags"] = current_flags
            self._reader_pane.update_message_flags(flags)

    def _on_reader_window_message_loaded(
        self,
        uid: str,
        account_uid: str,
        folder_name: str,
        msg: dict,
    ) -> None:
        if (
            self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
            and self._current_message_uid == uid
        ):
            self._current_message = msg
            body = {
                "plain": msg.get("body_plain"),
                "html": msg.get("body_html"),
            }
            self._reader_pane.show_message(
                msg,
                body=body,
                allow_remote=self._load_remote_content,
                dark=self._app_prefers_dark(),
                message_appearance=self._message_appearance,
            )
            self._reader_pane.set_actions_sensitive(True)
            if (msg.get("flags") or {}).get("seen"):
                self._mark_message_read(uid)

    def _notify_reader_windows_message_moved(self, uids: list[str]) -> None:
        for list_key in uids:
            location = self._message_location_for_list_key(list_key)
            message_uid = location[2] if location is not None else list_key
            for window in self._reader_windows:
                window.notify_message_moved(message_uid)

    def _open_uri_externally(self, uri: str) -> None:
        open_uri_externally(
            self,
            uri,
            on_error=lambda message: show_error_toast(self, message),
        )
