# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Main application window — 3-pane mail layout."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, WebKit

from post.compose_window import ComposeWindow
from post.credentials import prompt_password_sync
from post.icon_utils import apply_window_icon
from post.mail import MailService
from post.mail.eds import MailAccount, MessageNotAvailableError
from post.mail.sync_watcher import MailSyncWatcher
from post.message_list_view import VirtualMessageList
from post.mail.folders import POST_OUTBOX_FOLDER, is_post_outbox_folder
from post.mail.folder_index_cache import (
    has_cache as folder_index_has_cache,
    load as load_folder_index_cache,
)
from post.mail.message_list_state import message_list_fingerprint
from post.mail.search import MessageSearchQuery, parse_search_query
from post.mail.send_queue import (
    OFFLINE_CACHED_LIST_STATUS,
    OFFLINE_MAIL_MESSAGE,
    is_network_unavailable_error,
    list_queued_messages,
    list_queued_outbound_messages,
    log_mail_error,
    offline_status_text,
    read_queued_message,
    remove_queued_outbound_message,
)
from post.settings_window import SettingsWindow
from post.mail.helpers import (
    flag_menu_items,
    flag_menu_label,
    format_attachment_size,
    format_message_header,
    read_menu_items,
    read_menu_label,
    reader_toggle_button_state,
)
from post.reader import build_reader_document
from post.wrap_label import WrappingLabel, configure_ellipsize_label
from post.preferences import (
    MessageAppearance,
    get_auto_sync,
    get_load_remote_content,
    get_message_appearance,
    get_sidebar_state,
    get_window_state,
    set_active_message_uid,
    set_window_state,
)
from post.sidebar import MailSidebar
from post.toast import show_error_toast, show_toast

log = logging.getLogger(__name__)

_SIDEBAR_TOP_INSET = 12

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
expander.sidebar-section > title {{
  min-height: 0;
  padding-top: 4px;
  padding-bottom: 4px;
}}
expander.sidebar-section {{
  margin-top: 0;
  padding-top: 0;
}}
separator.header-divider {{
  min-height: 1px;
  background-color: alpha(@window_fg_color, 0.12);
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
button.message-flagged {{
  color: @error_color;
}}
button.message-read-action {{
  opacity: 1;
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
        self._close_after_outbound_send = False

        self._mail = MailService.connect()
        self._mail.set_password_prompt(self._prompt_account_password)
        self._sync_watcher = MailSyncWatcher(
            self._mail,
            on_folder_changed=self._on_sync_folder_changed,
            on_folder_tree_changed=self._on_sync_folder_tree_changed,
        )
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._current_message_uid: str | None = None
        self._current_message: dict | None = None
        self._current_body: dict[str, str | None] = {"plain": None, "html": None}
        self._messages_load_generation = 0
        self._message_read_generation = 0
        self._message_total = -1
        self._current_folder_messages: list[dict] | None = None
        self._message_list_source = ""
        self._message_sync_in_progress = False
        self._context_attachment_index: int | None = None
        self._context_attachment_mime: str | None = None
        self._context_attachment_name: str | None = None
        self._context_message_uids: list[str] = []
        self._pending_move_undo: dict | None = None
        self._undo_toast: Adw.Toast | None = None
        self._settings_dialog: SettingsWindow | None = None
        self._load_remote_content = get_load_remote_content()
        self._message_appearance = get_message_appearance()
        self._restore_message_folder: tuple[str, str] | None = None
        self._pending_restore_message_uid: str | None = None
        self._suppress_sync_list_reload: tuple[str, str] | None = None
        self._user_message_click_pending = False
        self._search_query: MessageSearchQuery | None = None
        self._search_entry_updating = False
        self._status_hint = ""
        self._network_available = Gio.NetworkMonitor.get_default().get_network_available()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_vexpand(True)

        header = Adw.HeaderBar()

        self._header_search_entry = Gtk.SearchEntry()
        self._header_search_entry.set_placeholder_text(
            "Search…  from: to: subject: cc: is:(!)read is:(!)flagged has:(!)attachment"
        )
        self._header_search_entry.set_size_request(546, -1)
        self._header_search_entry.set_hexpand(True)
        self._header_search_entry.set_sensitive(False)
        self._header_search_entry.set_search_delay(300)
        self._header_search_entry.connect("search-changed", self._on_search_changed)
        self._header_search_entry.connect("activate", self._on_search_activate)
        self._header_search_entry.connect("stop-search", self._on_search_stopped)
        search_title = Gtk.Box()
        search_title.set_halign(Gtk.Align.CENTER)
        search_title.set_hexpand(True)
        search_title.set_valign(Gtk.Align.CENTER)
        search_title.set_margin_start(48)
        search_title.set_margin_end(48)
        search_title.append(self._header_search_entry)
        header.set_title_widget(search_title)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings_clicked)
        header.pack_end(settings_btn)

        header_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header_actions.set_margin_end(10)
        header_actions.set_valign(Gtk.Align.CENTER)

        compose_btn = Gtk.Button(icon_name="mail-message-new-symbolic")
        compose_btn.set_tooltip_text("New Message (Ctrl+N)")
        compose_btn.connect("clicked", self._on_compose_new_clicked)
        header_actions.append(compose_btn)

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

        header.pack_end(header_actions)

        header_divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        header_divider.add_css_class("header-divider")
        outer.append(header_divider)

        content_panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content_panes.set_vexpand(True)
        outer.append(content_panes)

        self._sidebar = MailSidebar(
            self._mail,
            on_folder_selected=self._on_folder_selected,
            set_status=self._set_status,
            on_refresh_account=self._on_sidebar_refresh_account,
            on_refresh_folder=self._on_sidebar_refresh_folder,
            on_send_outbox=self._on_sidebar_send_outbox,
            on_accounts_loaded=self._on_accounts_loaded,
            on_folder_tree_changed=self._on_sidebar_folder_tree_changed,
            on_folder_contents_changed=self._on_sidebar_folder_contents_changed,
            on_move_started=self._on_sidebar_move_started,
            on_move_undo_available=self._on_sidebar_move_undo_available,
        )
        sidebar_widget = self._sidebar.widget
        sidebar_widget.set_margin_top(_SIDEBAR_TOP_INSET)
        content_panes.append(sidebar_widget)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep1.set_vexpand(True)
        content_panes.append(sep1)

        self._message_stack = Gtk.Stack()
        self._message_stack.set_size_request(320, -1)
        self._message_stack.set_hexpand(False)
        self._message_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._message_stack.set_transition_duration(150)

        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        loading_box.set_margin_start(24)
        loading_box.set_margin_end(24)
        self._message_loading_spinner = Gtk.Spinner()
        self._message_loading_spinner.set_size_request(32, 32)
        loading_box.append(self._message_loading_spinner)
        self._message_loading_label = Gtk.Label(label="Loading Messages…")
        self._message_loading_label.set_wrap(True)
        self._message_loading_label.add_css_class("dim-label")
        loading_box.append(self._message_loading_label)
        self._message_stack.add_named(loading_box, "loading")

        self._message_list_view = VirtualMessageList()
        self._message_scroll = self._message_list_view
        self._message_list_view.set_callbacks(
            on_selection_changed=self._on_message_list_selection_changed,
            on_item_activated=self._on_message_list_item_activated,
            on_item_pressed=self._on_message_list_item_pressed,
            on_item_context_menu=self._on_message_list_context_menu,
        )
        self._setup_message_shortcuts()

        message_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        message_panel.append(self._message_list_view)

        self._message_stack.add_named(message_panel, "list")

        empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        empty_box.set_margin_start(24)
        empty_box.set_margin_end(24)
        empty_icon = Gtk.Image.new_from_icon_name("mail-read-symbolic")
        empty_icon.set_pixel_size(48)
        empty_icon.add_css_class("dim-label")
        empty_box.append(empty_icon)
        self._message_empty_label = Gtk.Label(label="No Messages")
        self._message_empty_label.set_wrap(True)
        self._message_empty_label.add_css_class("dim-label")
        empty_box.append(self._message_empty_label)
        self._message_stack.add_named(empty_box, "empty")

        error_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        error_box.set_margin_start(24)
        error_box.set_margin_end(24)
        error_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        error_icon.set_pixel_size(48)
        error_icon.add_css_class("warning")
        error_box.append(error_icon)
        self._message_error_label = Gtk.Label(label="")
        self._message_error_label.set_wrap(True)
        self._message_error_label.set_justify(Gtk.Justification.CENTER)
        error_box.append(self._message_error_label)
        retry_btn = Gtk.Button(label="Try Again")
        retry_btn.connect("clicked", self._on_refresh)
        error_box.append(retry_btn)
        self._message_stack.add_named(error_box, "error")

        self._message_stack.set_visible_child_name("list")

        content_panes.append(self._message_stack)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_vexpand(True)
        content_panes.append(sep2)

        reader = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        reader.set_hexpand(True)
        reader.set_margin_start(16)
        reader.set_margin_end(16)
        reader.set_margin_top(_SIDEBAR_TOP_INSET)
        reader.set_margin_bottom(12)

        self._reader_subject = WrappingLabel(
            label="",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._reader_subject.add_css_class("title-2")
        self._reader_subject.set_hexpand(True)
        self._reader_subject.set_halign(Gtk.Align.FILL)
        self._reader_subject.set_visible(False)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.set_hexpand(True)
        subject_box = Gtk.Box()
        subject_box.set_hexpand(True)
        subject_box.append(self._reader_subject)
        header_row.append(subject_box)
        self._message_actions = self._build_message_action_buttons()
        self._message_actions.set_valign(Gtk.Align.START)
        self._message_actions.set_halign(Gtk.Align.END)
        header_row.append(self._message_actions)
        reader.append(header_row)

        self._reader_meta = Gtk.Label(
            label="",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._reader_meta.add_css_class("dim-label")
        self._reader_meta.set_width_chars(1)
        self._reader_meta.set_hexpand(True)
        self._reader_meta.set_halign(Gtk.Align.FILL)
        reader.append(self._reader_meta)

        self._reader_attachments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._reader_attachments.set_visible(False)
        reader.append(self._reader_attachments)

        self._reader_body_stack = Gtk.Stack()
        self._reader_body_stack.set_vexpand(True)
        self._reader_body_stack.set_hexpand(True)

        reader_empty_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        reader_empty_label = Gtk.Label(label="No Message Selected")
        reader_empty_label.add_css_class("dim-label")
        reader_empty_box.append(reader_empty_label)
        self._reader_body_stack.add_named(reader_empty_box, "empty")

        self._web_view = WebKit.WebView()
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        self._web_view.connect("decide-policy", self._on_web_view_decide_policy)
        self._web_view.set_vexpand(True)
        self._reader_body_stack.add_named(self._web_view, "content")
        self._reader_body_stack.set_visible_child_name("empty")

        reader.append(self._reader_body_stack)

        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._on_app_dark_changed)

        content_panes.append(reader)

        self._status = Gtk.Label(label="", xalign=0, margin_start=12, margin_bottom=6)
        self._status.add_css_class("dim-label")
        outer.append(self._status)

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
        self._clear_reader()

    def _setup_send_queue_flush(self) -> None:
        self._mail.set_network_available(self._network_available)
        monitor = Gio.NetworkMonitor.get_default()
        monitor.connect("notify::network-available", self._on_network_available_changed)
        self._refresh_status_display()
        GLib.timeout_add_seconds(2, self._flush_send_queue_on_startup)

    def _flush_send_queue_on_startup(self) -> bool:
        self._flush_send_queue_idle()
        return False

    def _on_network_available_changed(self, monitor: Gio.NetworkMonitor, *_args) -> None:
        online = monitor.get_network_available()
        if online == self._network_available:
            return
        self._network_available = online
        self._mail.set_network_available(online)
        self._refresh_status_display()
        if online:
            self._mail.go_online_sync()
            self._flush_send_queue_idle()
            if self._current_account and self._current_folder:
                if get_auto_sync():
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

    def _flush_send_queue_idle(self) -> bool:
        threading.Thread(target=self._flush_send_queue_worker, daemon=True).start()
        return False

    def _flush_send_queue_worker(self) -> None:
        try:
            sent = self._mail.flush_send_queue()
        except Exception:
            log.exception("Failed to flush outbound send queue")
            return
        if sent <= 0:
            return
        GLib.idle_add(self._on_send_queue_flushed, sent)

    def _on_send_queue_flushed(self, sent: int) -> bool:
        self._on_outbox_changed()
        if sent <= 0:
            return False
        if sent == 1:
            self._set_status("Sent 1 queued message")
        else:
            self._set_status(f"Sent {sent} queued messages")
        return False

    def _on_outbox_changed(self) -> None:
        self._sidebar.refresh_outbox_rows()
        self._refresh_status_display()
        if (
            self._current_account
            and is_post_outbox_folder(self._current_folder)
        ):
            self._load_messages(self._current_account.uid, POST_OUTBOX_FOLDER)

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
        set_window_state(
            width=width,
            height=height,
            maximized=self.is_maximized(),
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

        return self._finish_close()

    def _continue_close_after_outbound_send(self) -> None:
        self._close_after_outbound_send = False
        GLib.idle_add(self._destroy_after_close_cleanup)

    def _destroy_after_close_cleanup(self) -> bool:
        self._finish_close()
        self.destroy()
        return False

    def _finish_close(self) -> bool:
        self._sync_watcher.stop()
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

    def _build_message_action_buttons(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        flag_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        flag_group.add_css_class("linked")

        self._read_toggle_btn = self._make_message_action_button(
            "mail-mark-read-symbolic",
            "Mark as Read",
            self._on_read_toggle_clicked,
        )
        self._read_toggle_btn.add_css_class("message-read-action")
        self._flag_toggle_btn = self._make_message_action_button(
            "mail-flag-symbolic",
            "Flag",
            self._on_flag_toggle_clicked,
        )
        self._flag_toggle_btn.add_css_class("message-flagged")
        flag_group.append(self._read_toggle_btn)
        flag_group.append(self._flag_toggle_btn)
        outer.append(flag_group)

        reply_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        reply_group.add_css_class("linked")

        self._reply_btn = self._make_message_action_button(
            "mail-reply-sender-symbolic",
            "Reply",
            self._on_reply_clicked,
        )
        self._reply_all_btn = self._make_message_action_button(
            "mail-reply-all-symbolic",
            "Reply All",
            self._on_reply_all_clicked,
        )
        self._forward_btn = self._make_message_action_button(
            "mail-forward-symbolic",
            "Forward",
            self._on_forward_clicked,
        )
        reply_group.append(self._reply_btn)
        reply_group.append(self._reply_all_btn)
        reply_group.append(self._forward_btn)
        outer.append(reply_group)
        return outer

    @staticmethod
    def _make_message_action_button(
        icon_name: str, tooltip: str, handler: Callable[..., None]
    ) -> Gtk.Button:
        button = Gtk.Button()
        button.set_icon_name(icon_name)
        button.set_tooltip_text(tooltip)
        button.set_sensitive(False)
        button.connect("clicked", handler)
        return button

    def _set_message_actions_sensitive(self, sensitive: bool) -> None:
        self._read_toggle_btn.set_sensitive(sensitive)
        self._flag_toggle_btn.set_sensitive(sensitive)
        self._reply_btn.set_sensitive(sensitive)
        self._reply_all_btn.set_sensitive(sensitive)
        self._forward_btn.set_sensitive(sensitive)
        if sensitive:
            self._update_reader_toggle_buttons()

    def _reader_message_flags(self) -> dict:
        if self._current_message_uid is None:
            return {}
        return self._message_flags_for_uid(self._current_message_uid)

    def _update_reader_toggle_buttons(self) -> None:
        toggles = reader_toggle_button_state(self._reader_message_flags())
        for button, state in (
            (self._read_toggle_btn, toggles["read"]),
            (self._flag_toggle_btn, toggles["flag"]),
        ):
            button.set_icon_name(state["icon"])
            button.set_tooltip_text(state["tooltip"])
            if state["styled_action"]:
                button.add_css_class(state["action_class"])
            else:
                button.remove_css_class(state["action_class"])

    def _reader_action_uid(self) -> str | None:
        if self._current_message_uid is None:
            return None
        selected = self._message_list_view.get_selected_uids()
        if len(selected) != 1 or selected[0] != self._current_message_uid:
            return None
        return self._current_message_uid

    def _ensure_uid_selected(self, uid: str) -> None:
        if uid not in self._message_list_view.get_selected_uids():
            self._message_list_view.select_uid(uid)

    def _ensure_uid_selected_idle(self, uid: str) -> bool:
        self._ensure_uid_selected(uid)
        return False

    def _on_read_toggle_clicked(self, *_args) -> None:
        uid = self._reader_action_uid()
        if uid is None:
            return
        flags = self._message_flags_for_uid(uid)
        self._set_message_flags("seen", seen=not flags.get("seen", True), uids=[uid])
        GLib.idle_add(self._ensure_uid_selected_idle, uid)

    def _on_flag_toggle_clicked(self, *_args) -> None:
        uid = self._reader_action_uid()
        if uid is None:
            return
        flags = self._message_flags_for_uid(uid)
        self._set_message_flags(
            "flagged",
            flagged=not flags.get("flagged", False),
            uids=[uid],
        )
        GLib.idle_add(self._ensure_uid_selected_idle, uid)

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
        if self._settings_dialog is not None:
            self._settings_dialog.present(self)
            return
        dialog = SettingsWindow(
            parent=self,
            mail=self._mail,
            set_status=self._set_status,
            on_saved=self._reload_sidebar,
            on_load_remote_content_changed=self._on_load_remote_content_changed,
            on_auto_sync_changed=self._on_auto_sync_changed,
            on_message_appearance_changed=self._on_message_appearance_changed,
        )
        self._settings_dialog = dialog
        dialog.connect("closed", self._on_settings_closed)
        dialog.present(self)

    def _on_settings_closed(self, *_args) -> None:
        self._settings_dialog = None

    def _on_sidebar_refresh_account(self, account_uid: str) -> None:
        self._sidebar.reload_account(account_uid)

    def _on_sidebar_refresh_folder(self, account_uid: str, folder_name: str) -> None:
        self._sidebar.refresh_folder_row(account_uid, folder_name)
        if (
            self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
        ):
            self._load_messages(account_uid, folder_name, sync=True)

    def _on_sidebar_send_outbox(self) -> None:
        self._flush_send_queue_idle()

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

    def _on_sidebar_move_started(self, account_uid: str, folder_name: str) -> None:
        self._clear_move_undo()

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
        if get_auto_sync() and not self._sync_watcher.running:
            self._sync_watcher.start()

    def _on_auto_sync_changed(self, enabled: bool) -> None:
        if enabled:
            if self._current_account and self._current_folder:
                self._sync_watcher.set_current_folder(
                    self._current_account.uid, self._current_folder
                )
            if not self._sync_watcher.running:
                self._sync_watcher.start()
        else:
            self._sync_watcher.stop()

    def _on_sync_folder_changed(self, account_uid: str, folder_name: str) -> None:
        self._mail.invalidate_folder_index(account_uid, folder_name)
        self._refresh_folder_view(account_uid, folder_name)

    def _on_sync_folder_tree_changed(self, account_uid: str) -> None:
        self._sidebar.reload_account(account_uid)
        self._on_sidebar_folder_tree_changed(account_uid, None)

    def _refresh_folder_view(self, account_uid: str, folder_name: str) -> None:
        self._sidebar.refresh_folder_row(account_uid, folder_name)
        if (
            self._current_account
            and self._current_folder
            and self._current_account.uid == account_uid
            and self._current_folder == folder_name
        ):
            if self._suppress_sync_list_reload == (account_uid, folder_name):
                self._suppress_sync_list_reload = None
                return
            self._load_messages(account_uid, folder_name, sync=True)

    def _on_load_remote_content_changed(self, enabled: bool) -> None:
        self._load_remote_content = enabled
        if self._current_body.get("html") or self._current_body.get("plain"):
            self._show_reader_document()

    def _on_message_appearance_changed(self, appearance: MessageAppearance) -> None:
        self._message_appearance = appearance
        if self._current_body.get("html") or self._current_body.get("plain"):
            self._show_reader_document()

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

    def _open_compose_on_message(self, mode: str) -> None:
        if (
            not self._current_account
            or not self._current_folder
            or not self._current_message_uid
        ):
            prompt = "Select a message to forward" if mode == "forward" else "Select a message to reply"
            self._set_status(prompt)
            return
        if not self._mail.list_sendable_accounts():
            self._set_status("No mail account configured for sending")
            return
        account = self._current_account
        if not account.can_send:
            self._set_status("Selected account has no mail transport configured")
            return
        if self._current_message is not None:
            self._present_compose_window(
                account, mode=mode, reply_to=self._current_message
            )
            return

        account_uid = account.uid
        folder_name = self._current_folder
        message_uid = self._current_message_uid
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
                self._on_compose_message_loaded, account, msg, error, mode
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_compose_message_loaded(
        self,
        account: MailAccount,
        msg: dict | None,
        error: Exception | None,
        mode: str,
    ) -> bool:
        if error is not None:
            if isinstance(error, MessageNotAvailableError):
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
        self._present_compose_window(account, mode=mode, reply_to=msg)
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
    ) -> None:
        window = ComposeWindow(
            parent=self,
            mail=self._mail,
            account=account,
            set_status=self._set_status,
            on_outbox_changed=self._on_outbox_changed,
            on_draft_saved=self._on_draft_saved,
            mode=mode,  # type: ignore[arg-type]
            reply_to=reply_to,
            draft_folder_name=draft_folder_name,
            draft_message_uid=draft_message_uid,
            draft_message=draft_message,
        )
        window.present()

    def _on_draft_saved(self) -> None:
        if self._current_account is None or self._current_folder is None:
            return
        self._load_messages(
            self._current_account.uid,
            self._current_folder,
            sync=self._network_available and get_auto_sync(),
        )

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

        menu = Gio.Menu()
        menu.append("Save...", "win.attachment-save")
        menu.append("Open With…", "win.attachment-open-with")
        self._attachment_popover = Gtk.PopoverMenu.new_from_model(menu)

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
        GLib.idle_add(self._reload_sidebar)

    def _set_status(self, text: str) -> None:
        self._status_hint = text
        self._refresh_status_display()

    def _refresh_status_display(self) -> None:
        if not self._network_available:
            queued = len(list_queued_outbound_messages())
            self._status.set_label(offline_status_text(queued_count=queued))
            return
        self._status.set_label(self._status_hint)

    def _prompt_account_password(
        self, account_label: str, _mechanism: str | None
    ) -> str | None:
        return prompt_password_sync(self, account_label)

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

    def _update_search_entry_state(self) -> None:
        enabled = (
            self._current_account is not None
            and self._current_folder is not None
            and not is_post_outbox_folder(self._current_folder)
        )
        self._header_search_entry.set_sensitive(enabled)
        if not enabled:
            self._search_entry_updating = True
            self._header_search_entry.set_text("")
            self._search_entry_updating = False
            self._search_query = None

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

    def _apply_search_from_entry(self) -> None:
        if self._search_entry_updating:
            return
        if not self._current_account or not self._current_folder:
            return

        raw = self._header_search_entry.get_text()
        query = parse_search_query(raw)
        if query is None:
            if self._search_query is not None:
                self._search_query = None
                self._load_messages(
                    self._current_account.uid, self._current_folder, offset=0
                )
            return

        self._search_query = query
        self._load_messages(
            self._current_account.uid, self._current_folder, offset=0
        )

    def _on_search_stopped(self, _entry: Gtk.SearchEntry) -> None:
        if self._search_entry_updating:
            return
        self._exit_search_mode()

    def _exit_search_mode(self) -> None:
        self._search_query = None
        if self._current_account and self._current_folder:
            self._load_messages(
                self._current_account.uid, self._current_folder, offset=0
            )

    def _on_folder_selected(self, account: MailAccount, folder_name: str) -> None:
        self._current_account = account
        self._current_folder = folder_name
        if self._sync_watcher.running:
            self._sync_watcher.set_current_folder(account.uid, folder_name)
        self._update_search_entry_state()
        selection = (account.uid, folder_name)
        sidebar_state = get_sidebar_state()
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
        self._search_query = self._parse_search_from_entry()
        self._load_messages(account.uid, folder_name)

    def _show_message_unavailable_reader(self, message: str) -> None:
        self._reader_subject.set_label("Message unavailable")
        self._reader_subject.set_visible(True)
        self._reader_meta.set_label(message)
        self._clear_attachments()
        self._current_message = None
        self._message_actions.set_visible(False)
        self._set_message_actions_sensitive(False)
        self._current_body = {"plain": None, "html": None}
        self._reader_body_stack.set_visible_child_name("content")
        error_color = "#aaaaaa" if self._app_prefers_dark() else "#666666"
        self._web_view.load_html(
            "<body style='font-family:sans-serif;"
            f"color:{error_color};padding:1em'>"
            f"{message}</body>",
            None,
        )

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
                if message.get("uid") != uid
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
        self._reader_subject.set_label("")
        self._reader_subject.set_visible(False)
        self._reader_meta.set_label("")
        self._current_message_uid = None
        self._current_message = None
        self._message_actions.set_visible(False)
        self._set_message_actions_sensitive(False)
        self._update_message_toolbar()
        self._clear_attachments()
        self._current_body = {"plain": None, "html": None}
        self._show_reader_document()

    def _clear_attachments(self) -> None:
        while child := self._reader_attachments.get_first_child():
            self._reader_attachments.remove(child)
        self._reader_attachments.set_visible(False)

    def _show_attachments(self, attachments: list[dict]) -> None:
        self._clear_attachments()
        if not attachments:
            return

        heading = Gtk.Label(label="Attachments", xalign=0)
        heading.add_css_class("heading")
        self._reader_attachments.append(heading)

        list_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        list_column.set_hexpand(True)
        list_column.set_halign(Gtk.Align.FILL)

        for attachment in attachments:
            index = attachment.get("index", 0)
            name = attachment.get("filename") or "attachment"
            mime_type = attachment.get("mime_type")
            size = format_attachment_size(attachment.get("size"))
            label_text = f"{name} ({size})" if size else name

            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open Attachment")
            btn.set_hexpand(True)
            btn.set_halign(Gtk.Align.FILL)
            btn.connect("clicked", self._on_attachment_clicked, index)

            menu_gesture = Gtk.GestureClick()
            menu_gesture.set_button(Gdk.BUTTON_SECONDARY)
            menu_gesture.connect(
                "pressed",
                self._on_attachment_menu_pressed,
                index,
                mime_type,
                name,
            )
            btn.add_controller(menu_gesture)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
            icon.add_css_class("dim-label")
            label = Gtk.Label(label=label_text, xalign=0, ellipsize=3)
            configure_ellipsize_label(label)
            label.set_halign(Gtk.Align.FILL)
            row.append(icon)
            row.append(label)
            btn.set_child(row)
            list_column.append(btn)

        self._reader_attachments.append(list_column)
        self._reader_attachments.set_visible(True)

    def _on_attachment_menu_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        index: int,
        mime_type: str | None,
        name: str,
    ) -> None:
        self._context_attachment_index = index
        self._context_attachment_mime = mime_type
        self._context_attachment_name = name
        widget = gesture.get_widget()
        if widget is None:
            return
        self._ensure_popover_parent(self._attachment_popover, widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._attachment_popover.set_pointing_to(rect)
        self._attachment_popover.popup()

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

    def _fetch_attachment(
        self,
        attachment_index: int,
        on_ready: Callable[[str, bytes | None, Exception | None], None],
    ) -> None:
        if (
            not self._current_account
            or not self._current_folder
            or not self._current_message_uid
        ):
            return

        account_uid = self._current_account.uid
        folder_name = self._current_folder
        message_uid = self._current_message_uid

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

        threading.Thread(target=worker, daemon=True).start()

    def _on_attachment_fetched(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
        on_ready: Callable[[str, bytes | None, Exception | None], None],
    ) -> bool:
        on_ready(filename, data, error)
        return False

    def _on_attachment_clicked(self, _button: Gtk.Button, attachment_index: int) -> None:
        self._fetch_attachment(attachment_index, self._open_attachment_direct)

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
            path = self._write_temp_attachment(filename, data)
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
            path = self._write_temp_attachment(filename, data)
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

    @staticmethod
    def _write_temp_attachment(filename: str, data: bytes) -> str:
        directory = os.path.join(GLib.get_tmp_dir(), "post")
        os.makedirs(directory, exist_ok=True)
        basename = os.path.basename(filename.replace("/", "_").replace("\\", "_")) or "attachment"
        path = os.path.join(directory, basename)
        if os.path.exists(path):
            stem, ext = os.path.splitext(basename)
            counter = 1
            while os.path.exists(path):
                path = os.path.join(directory, f"{stem}-{counter}{ext}")
                counter += 1
        with open(path, "wb") as handle:
            handle.write(data)
        return path

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

        threading.Thread(target=worker_sync, daemon=True).start()

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
    ) -> str:
        if viewing_outbox:
            return "outbox"
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
        detail = self._load_source_label(source)
        if detail:
            return f"{action} {display_folder} {detail}…"
        return f"{action} {display_folder}…"

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
            parts.append("Syncing with Server")
        return " · ".join(parts)

    def _with_load_status_detail(self, text: str) -> str:
        detail = self._message_load_status_detail()
        if detail:
            return f"{text} · {detail}"
        return text

    def _load_messages(
        self,
        account_uid: str,
        folder_name: str,
        *,
        offset: int = 0,
        sync: bool | None = None,
    ) -> None:
        account = self._current_account
        if account is None or account.uid != account_uid:
            return
        if offset != 0:
            return

        if sync is None:
            sync = get_auto_sync()
        if not self._network_available:
            sync = False

        self._messages_load_generation += 1
        load_id = self._messages_load_generation

        display_folder = (
            "Outbox" if is_post_outbox_folder(folder_name) else folder_name
        )
        search_query = self._search_query
        viewing_outbox = is_post_outbox_folder(folder_name)
        should_sync = sync
        use_background_sync = (
            should_sync
            and self._network_available
            and not viewing_outbox
            and search_query is None
        )
        from_label = account.email or account.display_label

        def fetch_messages(sync_flag: bool) -> tuple[list[dict], int, int, str]:
            if viewing_outbox:
                messages, unread, total = list_queued_messages(
                    account_uid,
                    from_label=from_label,
                )
                return messages, unread, total, "outbox"
            if search_query is not None:
                messages, unread, total, source = self._mail.search_folder_messages(
                    account_uid,
                    folder_name,
                    search_query,
                    sync=sync_flag,
                )
                return messages, unread, total, source
            messages, unread, total, source = self._mail.get_folder_messages(
                account_uid,
                folder_name,
                sync=sync_flag,
            )
            return messages, unread, total, source

        self._message_total = -1
        self._current_folder_messages = None
        self._message_sync_in_progress = False
        initial_source = self._predict_initial_load_source(
            account_uid,
            folder_name,
            viewing_outbox=viewing_outbox,
            should_sync=should_sync,
            use_background_sync=use_background_sync,
        )
        self._message_list_source = initial_source
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
        self._clear_reader()

        cache_snapshot: tuple[list[dict], int, int] | None = None
        if not viewing_outbox and search_query is None:
            cache_snapshot = load_folder_index_cache(account_uid, folder_name)

        send_pending = self._mail.outbound_sends_pending()
        defer_mail_io = send_pending and cache_snapshot is None
        sync_after_send = use_background_sync and send_pending

        if cache_snapshot is not None:
            cached_messages, cached_unread, cached_total = cache_snapshot
            self._on_messages_loaded(
                load_id,
                account_uid,
                folder_name,
                list(cached_messages),
                cached_unread,
                cached_total,
                "disk_cache",
                sync_after_send,
                None,
            )
        else:
            self._message_loading_label.set_label(loading_label)
            self._message_loading_spinner.start()
            self._message_stack.set_visible_child_name("loading")

        def worker_initial() -> None:
            if cache_snapshot is not None and not (should_sync and not use_background_sync):
                return
            error: Exception | None = None
            messages: list[dict] | None = None
            unread = -1
            total = -1
            source = initial_source
            initial_sync = should_sync and not use_background_sync
            try:
                messages, unread, total, source = fetch_messages(initial_sync)
            except Exception as exc:
                if (
                    not viewing_outbox
                    and search_query is None
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
            threading.Thread(target=worker_initial, daemon=True).start()

        if defer_mail_io:

            def start_after_send() -> None:
                if load_id != self._messages_load_generation:
                    return
                start_initial_worker()

            self._mail.when_outbound_sends_complete(start_after_send)
        else:
            start_initial_worker()

        if use_background_sync and not send_pending:
            self._start_background_message_sync(
                load_id,
                account_uid,
                folder_name,
                fetch_messages,
            )
        elif sync_after_send:

            def sync_after_send() -> None:
                if load_id != self._messages_load_generation:
                    return
                self._start_background_message_sync(
                    load_id,
                    account_uid,
                    folder_name,
                    fetch_messages,
                )

            self._mail.when_outbound_sends_complete(sync_after_send)

    def _on_messages_sync_finished(self, load_id: int, changed: bool) -> bool:
        if load_id != self._messages_load_generation:
            return False
        self._message_sync_in_progress = False
        if changed:
            return False
        account = self._current_account
        if account is not None and self._current_folder is not None:
            self._update_message_status(account, self._current_folder)
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
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        self._message_loading_spinner.stop()

        if error is not None:
            if is_network_unavailable_error(error):
                if (
                    self._search_query is None
                    and not is_post_outbox_folder(folder_name)
                    and folder_index_has_cache(account_uid, folder_name)
                ):
                    try:
                        messages, unread, total, source = self._mail.get_folder_messages(
                            account_uid,
                            folder_name,
                            sync=False,
                        )
                    except Exception:
                        messages = None
                    else:
                        GLib.idle_add(
                            self._on_messages_loaded,
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
                        return False
                self._message_empty_label.set_label(OFFLINE_MAIL_MESSAGE)
                self._message_stack.set_visible_child_name("empty")
            else:
                self._message_error_label.set_label(str(error))
                self._message_stack.set_visible_child_name("error")
                show_error_toast(self, f"Could not load {folder_name}")
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            self._message_stack.set_visible_child_name("list")
            return False

        if not self._search_query:
            if is_post_outbox_folder(folder_name):
                self._sidebar.refresh_outbox_row(account_uid)
            else:
                self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        self._current_folder_messages = messages
        self._message_total = total
        self._message_list_source = source
        self._message_sync_in_progress = sync_pending

        if not messages:
            folder_label = "Outbox" if is_post_outbox_folder(folder_name) else folder_name
            if is_post_outbox_folder(folder_name):
                self._message_empty_label.set_label("No Queued Messages")
            elif self._search_query is not None:
                self._message_empty_label.set_label(f"No Matches in {folder_label}")
            else:
                self._message_empty_label.set_label(f"No Messages in {folder_label}")
            self._message_stack.set_visible_child_name("empty")
            self._update_message_status(account, folder_name)
            return False

        self._message_stack.set_visible_child_name("list")
        self._apply_folder_messages(messages, folder_name, account=account)
        if self._search_query is None:
            self._try_restore_selected_message(account.uid, folder_name)
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
        if error is not None:
            self._message_sync_in_progress = False
            if account := self._current_account:
                if self._current_folder == folder_name:
                    self._update_message_status(account, folder_name)
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            return False
        if self._current_folder != folder_name:
            return False

        if not self._search_query:
            if is_post_outbox_folder(folder_name):
                self._sidebar.refresh_outbox_row(account_uid)
            else:
                self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        current = self._current_folder_messages or []
        if (
            message_list_fingerprint(messages) == message_list_fingerprint(current)
            and self._message_total == total
        ):
            GLib.idle_add(self._on_messages_sync_finished, load_id, False)
            return False

        self._current_folder_messages = messages
        self._message_total = total
        self._message_list_source = "server"
        self._message_sync_in_progress = False
        self._messages_load_generation += 1
        self._set_status(f"Refreshing {folder_name} from server…")

        if not messages:
            folder_label = "Outbox" if is_post_outbox_folder(folder_name) else folder_name
            if is_post_outbox_folder(folder_name):
                self._message_empty_label.set_label("No Queued Messages")
            elif self._search_query is not None:
                self._message_empty_label.set_label(f"No Matches in {folder_label}")
            else:
                self._message_empty_label.set_label(f"No Messages in {folder_label}")
            self._message_list_view.clear()
            self._message_stack.set_visible_child_name("empty")
            self._update_message_status(account, folder_name)
            return False

        self._message_stack.set_visible_child_name("list")
        self._apply_folder_messages(messages, folder_name, account=account)
        if self._search_query is None:
            self._try_restore_selected_message(account.uid, folder_name)
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

    def _mark_message_read(self, uid: str) -> None:
        flags = self._message_flags_for_uid(uid)
        flags["seen"] = True
        self._message_list_view.update_message_flags(uid, flags)

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

        can_archive = False
        can_trash = False
        if self._current_account and self._current_folder:
            state = self._sidebar.get_move_menu_state(
                self._current_account.uid, self._current_folder
            )
            can_archive = bool(state.get("can_archive"))
            can_trash = bool(state.get("can_trash"))

        self._archive_action.set_enabled(can_archive)
        self._trash_action.set_enabled(can_trash)

        menu = Gio.Menu()
        count = len(uids)
        viewing_outbox = is_post_outbox_folder(self._current_folder or "")
        flags_for_uid = self._message_flags_for_uid
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
            remove_queued_outbound_message(queue_id)
        count = len(queue_ids)
        if count == 1:
            self._set_status("Removed 1 queued message")
        else:
            self._set_status(f"Removed {count} queued messages")
        self._on_outbox_changed()

    def _move_messages(self, destination: str, uids: list[str]) -> None:
        if not uids or not self._current_account or not self._current_folder:
            return

        state = self._sidebar.get_move_menu_state(
            self._current_account.uid, self._current_folder
        )
        if destination == "archive" and not state.get("can_archive"):
            return
        if destination == "trash" and not state.get("can_trash"):
            return

        uids = list(uids)
        account_uid = self._current_account.uid
        folder_name = self._current_folder
        self._message_popover.popdown()

        if is_post_outbox_folder(folder_name) and destination == "trash":
            self._delete_queued_messages(uids)
            return

        self._clear_move_undo()
        self._suppress_sync_list_reload = (account_uid, folder_name)

        def worker() -> None:
            error: Exception | None = None
            result: dict | None = None
            try:
                if destination == "trash":
                    result = self._mail.move_messages_to_trash(
                        account_uid, folder_name, uids
                    )
                else:
                    result = self._mail.archive_messages(
                        account_uid, folder_name, uids
                    )
            except Exception as exc:
                log.exception("Failed to move messages to %s", destination)
                error = exc
            GLib.idle_add(
                self._on_messages_moved,
                account_uid,
                folder_name,
                uids,
                destination,
                result,
                error,
            )

        label = "Trash" if destination == "trash" else "Archive"
        self._set_status(f"Moving {len(uids)} message(s) to {label}…")
        threading.Thread(target=worker, daemon=True).start()

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

    def _register_move_undo(
        self,
        label: str,
        *,
        account_uid: str,
        source_folder: str,
        dest_folder: str,
        dest_uids: list[str],
    ) -> None:
        if not dest_uids:
            log.warning("Move succeeded but destination UIDs are unknown; undo disabled")
            return

        self._pending_move_undo = {
            "account_uid": account_uid,
            "source_folder": source_folder,
            "dest_folder": dest_folder,
            "dest_uids": dest_uids,
        }
        self._undo_move_action.set_enabled(True)

        toast = Adw.Toast.new(label)
        toast.set_button_label("Undo")
        toast.set_action_name("win.undo-move")
        toast.set_priority(Adw.ToastPriority.HIGH)
        toast.set_timeout(10)
        toast.connect("dismissed", self._on_move_undo_dismissed)
        self._undo_toast = toast
        self._toast_overlay.add_toast(toast)

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
            GLib.idle_add(self._on_move_undo_finished, undo, result, error)

        self._set_status("Restoring messages…")
        threading.Thread(target=worker, daemon=True).start()

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
        moved_count = len(result.get("moved_uids") or uids)
        if status_label is None:
            status_label = self._move_status_label(destination, moved_count)

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
    ) -> bool:
        suppress_key = (account_uid, folder_name)

        if error is not None:
            if self._suppress_sync_list_reload == suppress_key:
                self._suppress_sync_list_reload = None
            show_error_toast(self, f"Could not move messages: {error}")
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

        moved_count = len(result.get("moved_uids") or uids)
        count_delta = removed_count if removed_count > 0 else moved_count
        if self._message_total >= 0:
            self._message_total = max(0, self._message_total - count_delta)

        if removed_count > 0 and self._current_folder_messages:
            moved_uids = set(uids)
            self._current_folder_messages = [
                message
                for message in self._current_folder_messages
                if message.get("uid") not in moved_uids
            ]

        if removed_count == 0 and moved_count > 0:
            if (
                self._current_account
                and self._current_folder
                and self._current_account.uid == account_uid
                and self._current_folder == folder_name
            ):
                self._load_messages(account_uid, folder_name, sync=False)
        elif self._message_list_view.item_count() == 0 and folder_name:
            self._message_empty_label.set_label(
                f"No Messages in {folder_name}"
            )
            self._message_stack.set_visible_child_name("empty")

        self._update_sidebar_from_move_result(account_uid, result)
        self._finalize_move_status_and_undo(
            account_uid, folder_name, destination, uids, result
        )
        self._update_message_toolbar()

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
        if uids is None:
            uids = list(self._context_message_uids)
        if not uids or not self._current_account or not self._current_folder:
            return

        account_uid = self._current_account.uid
        folder_name = self._current_folder

        error: Exception | None = None
        result: dict | None = None
        try:
            if flag_name == "seen":
                assert seen is not None
                result = self._mail.set_messages_seen(
                    account_uid, folder_name, uids, seen=seen
                )
            else:
                assert flagged is not None
                result = self._mail.set_messages_flagged(
                    account_uid, folder_name, uids, flagged=flagged
                )
        except Exception as exc:
            log.exception("Failed to update message %s", flag_name)
            error = exc
        self._on_messages_flag_toggled(uids, flag_name, result, error)

    def _on_messages_flag_toggled(
        self,
        uids: list[str],
        flag_name: str,
        result: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Could not update messages: {error}")
            return False
        if result is None:
            return False

        updates_by_uid = {
            item["uid"]: item.get("flags") or {}
            for item in result.get("updates") or []
            if item.get("uid")
        }
        for uid in uids:
            if uid not in updates_by_uid:
                continue
            flags = dict(updates_by_uid[uid])
            self._message_list_view.update_message_flags(uid, flags)
            if uid == self._current_message_uid and self._current_message is not None:
                current_flags = dict(self._current_message.get("flags") or {})
                current_flags.update(flags)
                self._current_message["flags"] = current_flags

        if self._current_message_uid in updates_by_uid:
            self._update_reader_toggle_buttons()

        if self._current_account and self._current_folder:
            self._suppress_sync_list_reload = (
                self._current_account.uid,
                self._current_folder,
            )

        if flag_name == "seen" and self._current_account and self._current_folder:
            unread = result.get("folder_unread")
            total = result.get("folder_total")
            if unread is not None and total is not None:
                self._sidebar.update_folder_row(
                    self._current_account.uid,
                    self._current_folder,
                    unread,
                    total,
                )

        count = len(updates_by_uid)
        if count > 1:
            self._set_status(f"Updated {count} messages")
        return False

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
            return False

        uid = selected[0]
        if uid == self._current_message_uid and self._current_message is not None:
            return False

        mark_seen = self._user_message_click_pending
        self._user_message_click_pending = False
        self._current_message_uid = uid
        self._load_message_body_for_uid(uid, mark_seen=mark_seen)
        return False

    def _on_message_list_item_activated(self, uid: str) -> None:
        if self._message_list_view.is_restoring_selection():
            return
        mark_seen = self._user_message_click_pending
        self._user_message_click_pending = False
        if not self._current_account or not self._current_folder:
            return
        if len(self._message_list_view.get_selected_uids()) != 1:
            return
        if uid == self._current_message_uid and self._current_message is not None:
            return
        self._current_message_uid = uid
        self._load_message_body_for_uid(uid, mark_seen=mark_seen)

    def _update_message_toolbar(self) -> bool:
        selected = self._message_list_view.get_selected_uids()
        can_use_reader_actions = (
            len(selected) == 1
            and self._current_message_uid is not None
            and selected[0] == self._current_message_uid
        )
        self._set_message_actions_sensitive(can_use_reader_actions)

        has_selection = bool(selected)
        can_archive = False
        can_trash = False
        if has_selection and self._current_account and self._current_folder:
            state = self._sidebar.get_move_menu_state(
                self._current_account.uid, self._current_folder
            )
            can_archive = bool(state.get("can_archive"))
            can_trash = bool(state.get("can_trash"))

        self._header_archive_btn.set_sensitive(can_archive)
        self._header_trash_btn.set_sensitive(can_trash)
        return False

    def _load_message_body_for_uid(
        self,
        uid: str,
        *,
        mark_seen: bool,
    ) -> None:
        if not self._current_account or not self._current_folder:
            return

        account = self._current_account
        folder_name = self._current_folder
        self._message_read_generation += 1
        read_id = self._message_read_generation
        viewing_outbox = is_post_outbox_folder(folder_name)
        viewing_drafts = self._sidebar.folder_is_drafts(account.uid, folder_name)
        from_label = account.email or account.display_label

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                if viewing_outbox:
                    msg = read_queued_message(
                        uid,
                        account_uid=account.uid,
                        from_label=from_label,
                    )
                else:
                    msg = self._mail.read_message(
                        account.uid,
                        folder_name,
                        uid,
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
                log.exception("Failed to read message")
                error = exc
            if viewing_drafts:
                GLib.idle_add(
                    self._on_draft_message_loaded,
                    account,
                    folder_name,
                    uid,
                    msg,
                    error,
                )
            else:
                GLib.idle_add(
                    self._on_message_read,
                    read_id,
                    uid,
                    msg,
                    error,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_draft_message_loaded(
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

    def _on_message_read(
        self,
        read_id: int,
        uid: str,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if read_id != self._message_read_generation:
            return False

        if isinstance(error, MessageNotAvailableError):
            self._remove_vanished_message(uid)
            self._show_message_unavailable_reader(error.user_message())
            show_error_toast(self, error.user_message())
            return False

        if error is not None:
            self._reader_subject.set_label("Could not read message")
            self._reader_subject.set_visible(True)
            self._reader_meta.set_label(str(error))
            self._clear_attachments()
            self._current_message = None
            self._message_actions.set_visible(False)
            self._set_message_actions_sensitive(False)
            self._current_body = {"plain": None, "html": None}
            self._reader_body_stack.set_visible_child_name("content")
            error_color = "#aaaaaa" if self._app_prefers_dark() else "#666666"
            self._web_view.load_html(
                "<body style='font-family:sans-serif;"
                f"color:{error_color};padding:1em'>"
                "This message could not be loaded.</body>",
                None,
            )
            show_error_toast(self, f"Read error: {error}")
            return False

        assert msg is not None
        if not self._current_account or not self._current_folder:
            return False

        self._current_message_uid = uid
        set_active_message_uid(uid)
        self._restore_message_folder = (
            self._current_account.uid,
            self._current_folder,
        )
        if "folder_unread" in msg and "folder_total" in msg:
            self._sidebar.update_folder_row(
                self._current_account.uid,
                self._current_folder,
                msg["folder_unread"],
                msg["folder_total"],
            )
        if (msg.get("flags") or {}).get("seen"):
            self._mark_message_read(uid)

        self._reader_subject.set_label(msg.get("subject") or "(no subject)")
        self._reader_subject.set_visible(True)
        self._reader_meta.set_label(format_message_header(msg))
        self._current_message = msg
        self._message_actions.set_visible(True)
        self._set_message_actions_sensitive(True)
        self._show_attachments(msg.get("attachments") or [])
        self._current_body = {
            "plain": msg.get("body_plain"),
            "html": msg.get("body_html"),
        }
        self._show_reader_document()
        return False

    def _app_prefers_dark(self) -> bool:
        return Adw.StyleManager.get_default().get_dark()

    def _on_app_dark_changed(self, *_args) -> None:
        if self._current_message is not None:
            self._show_reader_document()

    def _show_reader_document(self) -> None:
        if self._current_message is None:
            self._reader_body_stack.set_visible_child_name("empty")
            return

        self._reader_body_stack.set_visible_child_name("content")
        document = build_reader_document(
            body_html=self._current_body.get("html"),
            body_plain=self._current_body.get("plain"),
            allow_remote=self._load_remote_content,
            dark=self._app_prefers_dark(),
            message_appearance=self._message_appearance,
            inline_images=self._current_message.get("inline_images"),
        )
        self._web_view.load_html(document, None)

    @staticmethod
    def _uri_opens_externally(uri: str) -> bool:
        lower = uri.lower()
        return lower.startswith(("http://", "https://", "mailto:"))

    def _open_uri_externally(self, uri: str) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            show_error_toast(self, f"Could not open link: {exc.message}")

    def _on_web_view_decide_policy(
        self,
        _web_view: WebKit.WebView,
        decision: WebKit.NavigationPolicyDecision,
        decision_type: WebKit.PolicyDecisionType,
    ) -> bool:
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False

        navigation = decision.get_navigation_action()
        if navigation is None:
            return False

        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav_type = navigation.get_navigation_type()
            if nav_type not in (
                WebKit.NavigationType.LINK_CLICKED,
                WebKit.NavigationType.FORM_SUBMITTED,
                WebKit.NavigationType.FORM_RESUBMITTED,
            ):
                return False

        request = navigation.get_request()
        if request is None:
            return False

        uri = request.get_uri()
        if not uri or not self._uri_opens_externally(uri):
            decision.ignore()
            return True

        self._open_uri_externally(uri)
        decision.ignore()
        return True
