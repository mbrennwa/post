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
from post.mail import MailService
from post.mail.eds import DEFAULT_MESSAGE_PAGE_SIZE, MailAccount
from post.settings_window import SettingsWindow
from post.mail.helpers import (
    format_attachment_size,
    format_message_header,
    format_message_list_date,
    message_has_attachments,
    message_is_flagged,
    message_is_unread,
)
from post.reader import build_reader_document
from post.sidebar import MailSidebar

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Post")
        self.set_default_size(1100, 720)

        self._mail = MailService.connect()
        self._mail.set_password_prompt(self._prompt_account_password)
        self._current_account: MailAccount | None = None
        self._current_folder: str | None = None
        self._current_message_uid: str | None = None
        self._current_message: dict | None = None
        self._current_body: dict[str, str | None] = {"plain": None, "html": None}
        self._messages_load_generation = 0
        self._message_read_generation = 0
        self._shown_message_count = 0
        self._message_total = -1
        self._message_has_more = False
        self._context_attachment_index: int | None = None
        self._context_attachment_mime: str | None = None
        self._context_attachment_name: str | None = None
        self._context_message_rows: list[Gtk.ListBoxRow] = []
        self._pending_move_undo: dict | None = None
        self._undo_toast: Adw.Toast | None = None
        self._settings_dialog: SettingsWindow | None = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_vexpand(True)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Post", subtitle="Mail")
        header.set_title_widget(title)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self._on_settings_clicked)
        header.pack_end(settings_btn)

        compose_btn = Gtk.Button()
        compose_btn.set_icon_name("mail-message-new-symbolic")
        compose_btn.set_label("New Message")
        compose_btn.set_tooltip_text("New Message")
        compose_btn.connect("clicked", self._on_compose_new_clicked)
        header.pack_end(compose_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_end(refresh_btn)

        remote_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        remote_label = Gtk.Label(label="Remote content")
        remote_label.add_css_class("dim-label")
        self._remote_switch = Gtk.Switch(active=False)
        self._remote_switch.set_tooltip_text("Load remote images and linked resources")
        self._remote_switch.connect("notify::active", self._on_remote_content_changed)
        remote_box.append(remote_label)
        remote_box.append(self._remote_switch)
        header.pack_end(remote_box)

        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        panes.set_vexpand(True)
        outer.append(panes)

        self._sidebar = MailSidebar(
            self._mail,
            on_folder_selected=self._on_folder_selected,
            set_status=self._set_status,
        )
        panes.append(self._sidebar.widget)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep1)

        self._message_stack = Gtk.Stack()
        self._message_stack.set_size_request(320, -1)
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
        self._message_loading_label = Gtk.Label(label="Loading messages…")
        self._message_loading_label.set_wrap(True)
        self._message_loading_label.add_css_class("dim-label")
        loading_box.append(self._message_loading_label)
        self._message_stack.add_named(loading_box, "loading")

        message_scroll = Gtk.ScrolledWindow()
        message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        message_scroll.set_vexpand(True)
        self._message_scroll = message_scroll
        self._message_list = Gtk.ListBox()
        self._message_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self._message_list.set_activate_on_single_click(False)
        self._message_list.connect("row-selected", self._on_message_list_selection_changed)
        self._message_list.connect("row-selected", self._on_message_selected)
        context_gesture = Gtk.GestureClick()
        context_gesture.set_button(0)
        context_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        context_gesture.connect("pressed", self._on_message_list_pressed)
        self._message_list.add_controller(context_gesture)
        self._setup_message_shortcuts()
        message_scroll.set_child(self._message_list)

        message_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        message_panel.append(message_scroll)
        self._load_more_btn = Gtk.Button(label="Load more")
        self._load_more_btn.set_margin_start(12)
        self._load_more_btn.set_margin_end(12)
        self._load_more_btn.set_margin_top(6)
        self._load_more_btn.set_margin_bottom(6)
        self._load_more_btn.set_visible(False)
        self._load_more_btn.connect("clicked", self._on_load_more)
        message_panel.append(self._load_more_btn)

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
        self._message_empty_label = Gtk.Label(label="No messages")
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
        retry_btn = Gtk.Button(label="Try again")
        retry_btn.connect("clicked", self._on_refresh)
        error_box.append(retry_btn)
        self._message_stack.add_named(error_box, "error")

        self._message_stack.set_visible_child_name("list")

        panes.append(self._message_stack)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        panes.append(sep2)

        reader = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        reader.set_hexpand(True)
        reader.set_margin_start(16)
        reader.set_margin_end(16)
        reader.set_margin_top(12)
        reader.set_margin_bottom(12)

        reader_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._reply_btn = Gtk.Button(label="Reply")
        self._reply_btn.set_sensitive(False)
        self._reply_btn.connect("clicked", self._on_reply_clicked)
        reader_toolbar.append(self._reply_btn)
        reader.append(reader_toolbar)

        self._reader_subject = Gtk.Label(
            label="Select a message",
            xalign=0,
            wrap=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        self._reader_subject.add_css_class("title-2")
        reader.append(self._reader_subject)

        self._reader_meta = Gtk.Label(label="", xalign=0, wrap=True)
        self._reader_meta.add_css_class("dim-label")
        reader.append(self._reader_meta)

        self._reader_attachments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._reader_attachments.set_visible(False)
        reader.append(self._reader_attachments)

        self._web_view = WebKit.WebView()
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        self._web_view.connect("decide-policy", self._on_web_view_decide_policy)
        self._web_view.set_vexpand(True)
        reader.append(self._web_view)

        panes.append(reader)

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

    def _setup_delete_shortcut(self) -> None:
        for widget in (self, self._message_list):
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
        rows = self._message_list.get_selected_rows()
        if not rows or not self._current_account or not self._current_folder:
            return False
        state = self._sidebar.get_move_menu_state(
            self._current_account.uid, self._current_folder
        )
        if not state.get("can_trash"):
            return False
        self._move_selected_messages("trash")
        return True

    def _setup_compose_action(self) -> None:
        compose_action = Gio.SimpleAction.new("compose-new", None)
        compose_action.connect("activate", self._on_compose_new_action)
        self.add_action(compose_action)

        reply_action = Gio.SimpleAction.new("compose-reply", None)
        reply_action.connect("activate", self._on_reply_action)
        self.add_action(reply_action)

        application = self.get_application()
        if application is not None:
            application.set_accels_for_action("win.compose-new", ["<Control>n"])

    def _on_compose_new_action(self, *_args) -> None:
        self._open_compose_new()

    def _on_compose_new_clicked(self, *_args) -> None:
        self._open_compose_new()

    def _on_settings_clicked(self, *_args) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.present(self)
            return
        dialog = SettingsWindow(
            parent=self,
            mail=self._mail,
            set_status=self._set_status,
            on_saved=self._reload_sidebar,
        )
        self._settings_dialog = dialog
        dialog.connect("closed", self._on_settings_closed)
        dialog.present(self)

    def _on_settings_closed(self, *_args) -> None:
        self._settings_dialog = None

    def _on_reply_action(self, *_args) -> None:
        self._open_compose_reply()

    def _on_reply_clicked(self, *_args) -> None:
        self._open_compose_reply()

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

    def _open_compose_reply(self) -> None:
        if (
            not self._current_account
            or not self._current_folder
            or not self._current_message_uid
        ):
            self._set_status("Select a message to reply")
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
                account, mode="reply", reply_to=self._current_message
            )
            return

        account_uid = account.uid
        folder_name = self._current_folder
        message_uid = self._current_message_uid
        self._set_status("Preparing reply…")

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                msg = self._mail.read_message(account_uid, folder_name, message_uid)
            except Exception as exc:
                log.exception("Failed to load message for reply")
                error = exc
            GLib.idle_add(self._on_reply_message_loaded, account, msg, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_reply_message_loaded(
        self,
        account: MailAccount,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            self._set_status(f"Could not prepare reply: {error}")
            return False
        if msg is None:
            return False
        self._current_message = msg
        self._reply_btn.set_sensitive(True)
        self._present_compose_window(account, mode="reply", reply_to=msg)
        return False

    def _present_compose_window(
        self,
        account: MailAccount,
        *,
        mode: str,
        reply_to: dict | None = None,
    ) -> None:
        window = ComposeWindow(
            parent=self,
            mail=self._mail,
            account=account,
            set_status=self._set_status,
            mode=mode,  # type: ignore[arg-type]
            reply_to=reply_to,
        )
        window.present()

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
        menu.append("Open with…", "win.attachment-open-with")
        self._attachment_popover = Gtk.PopoverMenu.new_from_model(menu)

    def _setup_message_menu(self) -> None:
        read_action = Gio.SimpleAction.new("message-toggle-read", None)
        read_action.connect("activate", self._on_message_menu_toggle_read)
        self.add_action(read_action)

        flag_action = Gio.SimpleAction.new("message-toggle-flag", None)
        flag_action.connect("activate", self._on_message_menu_toggle_flag)
        self.add_action(flag_action)

        self._archive_action = Gio.SimpleAction.new("message-archive", None)
        self._archive_action.connect("activate", self._on_message_menu_archive)
        self.add_action(self._archive_action)

        self._trash_action = Gio.SimpleAction.new("message-move-trash", None)
        self._trash_action.connect("activate", self._on_message_menu_move_trash)
        self.add_action(self._trash_action)

        reply_action = Gio.SimpleAction.new("message-reply", None)
        reply_action.connect("activate", self._on_message_menu_reply)
        self.add_action(reply_action)

        self._message_popover = Gtk.PopoverMenu.new_from_model(Gio.Menu())
        self._message_popover.set_parent(self._message_scroll)

    def _setup_message_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        for accelerator in ("Menu", "<Shift>F10"):
            trigger = Gtk.ShortcutTrigger.parse_string(accelerator)
            action = Gtk.CallbackAction.new(self._on_message_context_shortcut)
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))
        self._message_list.add_controller(controller)

    def _on_message_context_shortcut(
        self,
        _widget: Gtk.Widget,
        _args: GLib.Variant | None = None,
    ) -> bool:
        row = self._message_list.get_selected_row()
        if row is None:
            rows = self._message_list.get_selected_rows()
            row = rows[0] if rows else None
        if not isinstance(row, Gtk.ListBoxRow):
            return False
        allocation = row.get_allocation()
        self._popup_message_menu(row, allocation.width / 2, allocation.height / 2)
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
        self._status.set_label(text)

    def _prompt_account_password(
        self, account_label: str, _mechanism: str | None
    ) -> str | None:
        return prompt_password_sync(self, account_label)

    def _reload_sidebar(self) -> bool:
        self._clear_reader()
        self._message_popover.popdown()
        self._clear_listbox(self._message_list)
        self._current_account = None
        self._current_folder = None
        self._sidebar.load()
        return False

    def _on_folder_selected(self, account: MailAccount, folder_name: str) -> None:
        self._current_account = account
        self._current_folder = folder_name
        self._load_messages(account.uid, folder_name)

    def _clear_reader(self) -> None:
        self._reader_subject.set_label("Select a message")
        self._reader_meta.set_label("")
        self._current_message_uid = None
        self._current_message = None
        self._reply_btn.set_sensitive(False)
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

        for attachment in attachments:
            index = attachment.get("index", 0)
            name = attachment.get("filename") or "attachment"
            mime_type = attachment.get("mime_type")
            size = format_attachment_size(attachment.get("size"))
            label_text = f"{name} ({size})" if size else name

            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open attachment")
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
            label.set_hexpand(True)
            row.append(icon)
            row.append(label)
            btn.set_child(row)
            self._reader_attachments.append(btn)

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
            self._set_status(f"Attachment error: {error}")
            return
        if data is None:
            self._set_status("Attachment error: no data")
            return

        try:
            path = self._write_temp_attachment(filename, data)
            file = Gio.File.new_for_path(path)
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except (OSError, GLib.Error) as exc:
            self._set_status(f"Could not open attachment: {exc}")
            return

        self._set_status(f"Opened {os.path.basename(filename)}")

    def _prompt_save_attachment(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            self._set_status(f"Attachment error: {error}")
            return
        if data is None:
            self._set_status("Attachment error: no data")
            return

        dialog = Gtk.FileDialog(title="Save attachment")
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
            self._set_status(f"Save error: {exc.message}")
            return

        path = file.get_path()
        if path is None:
            self._set_status("Save error: no path")
            return

        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self._set_status(f"Save error: {exc}")
            return

        self._set_status(f"Saved {os.path.basename(filename)}")

    def _prompt_open_with_dialog(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            self._set_status(f"Attachment error: {error}")
            return
        if data is None:
            self._set_status("Attachment error: no data")
            return

        try:
            path = self._write_temp_attachment(filename, data)
        except OSError as exc:
            self._set_status(f"Could not open attachment: {exc}")
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
        dialog.set_heading("Open with")
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
                    self._set_status(f"Could not open attachment: {exc.message}")
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
            self._load_messages(self._current_account.uid, self._current_folder)
        else:
            GLib.idle_add(self._reload_sidebar)

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            if isinstance(child, Gtk.ListBoxRow):
                listbox.remove(child)
            child = next_child

    def _load_messages(
        self, account_uid: str, folder_name: str, *, offset: int = 0
    ) -> None:
        account = self._current_account
        if account is None or account.uid != account_uid:
            return

        if offset == 0:
            self._messages_load_generation += 1
            self._shown_message_count = 0
            self._message_total = -1
            self._message_has_more = False
            self._load_more_btn.set_visible(False)
            self._set_status(f"Loading {folder_name}…")
            self._message_popover.popdown()
            self._clear_listbox(self._message_list)
            self._clear_reader()
            self._message_loading_label.set_label(f"Loading {folder_name}…")
            self._message_loading_spinner.start()
            self._message_stack.set_visible_child_name("loading")
        else:
            self._load_more_btn.set_sensitive(False)
            self._load_more_btn.set_label("Loading…")

        load_id = self._messages_load_generation

        def worker() -> None:
            error: Exception | None = None
            messages: list[dict] | None = None
            unread = -1
            total = -1
            has_more = False
            try:
                messages, unread, total, has_more = self._mail.list_messages_page(
                    account_uid,
                    folder_name,
                    offset=offset,
                    limit=DEFAULT_MESSAGE_PAGE_SIZE,
                )
            except Exception as exc:
                log.exception("Failed to list messages")
                error = exc
            GLib.idle_add(
                self._on_messages_loaded,
                load_id,
                account_uid,
                folder_name,
                offset,
                messages,
                unread,
                total,
                has_more,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_load_more(self, *_args) -> None:
        if (
            not self._current_account
            or not self._current_folder
            or not self._message_has_more
        ):
            return
        self._load_messages(
            self._current_account.uid,
            self._current_folder,
            offset=self._shown_message_count,
        )

    def _on_messages_loaded(
        self,
        load_id: int,
        account_uid: str,
        folder_name: str,
        offset: int,
        messages: list[dict] | None,
        unread: int,
        total: int,
        has_more: bool,
        error: Exception | None,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        self._message_loading_spinner.stop()
        self._load_more_btn.set_sensitive(True)
        self._load_more_btn.set_label("Load more")

        if error is not None:
            self._message_error_label.set_label(str(error))
            self._message_stack.set_visible_child_name("error")
            self._set_status(f"Could not load {folder_name}")
            self._load_more_btn.set_visible(False)
            return False

        assert messages is not None
        account = self._current_account
        if account is None or account.uid != account_uid:
            self._message_stack.set_visible_child_name("list")
            return False

        if offset == 0:
            self._sidebar.update_folder_row(account_uid, folder_name, unread, total)

        self._message_total = total
        self._message_has_more = has_more

        if offset == 0 and not messages:
            folder_label = folder_name
            self._message_empty_label.set_label(f"No messages in {folder_label}")
            self._message_stack.set_visible_child_name("empty")
            self._update_message_status(account, folder_name)
            return False

        self._message_stack.set_visible_child_name("list")
        self._populate_message_rows(
            load_id,
            messages,
            0,
            account,
            folder_name,
            page_offset=offset,
        )
        return False

    def _populate_message_rows(
        self,
        load_id: int,
        messages: list[dict],
        row_offset: int,
        account: MailAccount,
        folder_name: str,
        page_offset: int,
        batch_size: int = 25,
    ) -> bool:
        if load_id != self._messages_load_generation:
            return False

        end = min(row_offset + batch_size, len(messages))
        for msg in messages[row_offset:end]:
            subject = msg.get("subject") or "(no subject)"
            sender = msg.get("from") or ""
            preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            preview.set_margin_start(12)
            preview.set_margin_end(12)
            preview.set_margin_top(8)
            preview.set_margin_bottom(8)

            top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            subject_label = Gtk.Label(label=subject, xalign=0, wrap=True)
            subject_label.set_hexpand(True)
            if message_is_unread(msg):
                subject_label.add_css_class("heading")
            top_row.append(subject_label)

            date_text = format_message_list_date(msg)
            if date_text:
                date_label = Gtk.Label(label=date_text, xalign=1)
                date_label.add_css_class("dim-label")
                top_row.append(date_label)
            preview.append(top_row)

            bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            meta = Gtk.Label(label=sender, xalign=0, ellipsize=3)
            meta.set_hexpand(True)
            meta.add_css_class("dim-label")
            bottom_row.append(meta)

            if message_has_attachments(msg):
                attach_icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
                attach_icon.add_css_class("dim-label")
                attach_icon.set_tooltip_text("Has attachments")
                bottom_row.append(attach_icon)

            flag_icon = Gtk.Image.new_from_icon_name("mail-mark-important-symbolic")
            flag_icon.add_css_class("dim-label")
            flag_icon.set_tooltip_text("Flagged")
            flag_icon.set_visible(message_is_flagged(msg))
            bottom_row.append(flag_icon)
            preview.append(bottom_row)

            row = Gtk.ListBoxRow()
            row.set_child(preview)
            row.message_uid = msg.get("uid")
            row.message_flags = dict(msg.get("flags") or {})
            row.subject_label = subject_label
            row.flag_icon = flag_icon

            self._message_list.append(row)

        if end < len(messages):
            GLib.idle_add(
                self._populate_message_rows,
                load_id,
                messages,
                end,
                account,
                folder_name,
                page_offset,
                batch_size,
            )
            return False

        self._shown_message_count = page_offset + len(messages)
        self._load_more_btn.set_visible(self._message_has_more)
        self._update_message_status(account, folder_name)
        return False

    def _update_message_status(self, account: MailAccount, folder_name: str) -> None:
        shown = self._shown_message_count
        total = self._message_total
        label = account.display_label
        if total >= 0 and shown < total:
            self._set_status(f"Showing {shown} of {total} in {label} / {folder_name}")
        elif total >= 0:
            self._set_status(f"{total} messages in {label} / {folder_name}")
        else:
            self._set_status(f"{shown} messages in {label} / {folder_name}")

    def _mark_row_read(self, row: Gtk.ListBoxRow) -> None:
        subject_label = getattr(row, "subject_label", None)
        if isinstance(subject_label, Gtk.Label):
            subject_label.remove_css_class("heading")
        flags = dict(getattr(row, "message_flags", {}) or {})
        flags["seen"] = True
        row.message_flags = flags

    def _message_rows_for_menu(self, row: Gtk.ListBoxRow) -> list[Gtk.ListBoxRow]:
        selected = self._message_list.get_selected_rows()
        if row in selected:
            return selected
        self._message_list.unselect_all()
        self._message_list.select_row(row)
        return [row]

    @staticmethod
    def _read_menu_label(rows: list[Gtk.ListBoxRow]) -> str:
        seen_states = [
            (getattr(row, "message_flags", {}) or {}).get("seen", True) for row in rows
        ]
        count = len(rows)
        suffix = f" ({count})" if count > 1 else ""
        if all(seen_states):
            return f"Mark as unread{suffix}"
        if not any(seen_states):
            return f"Mark as read{suffix}"
        return f"Toggle read{suffix}"

    @staticmethod
    def _flag_menu_label(rows: list[Gtk.ListBoxRow]) -> str:
        flagged_states = [
            (getattr(row, "message_flags", {}) or {}).get("flagged", False)
            for row in rows
        ]
        count = len(rows)
        suffix = f" ({count})" if count > 1 else ""
        if all(flagged_states):
            return f"Unflag{suffix}"
        if not any(flagged_states):
            return f"Flag{suffix}"
        return f"Toggle flag{suffix}"

    @staticmethod
    def _count_menu_label(base: str, rows: list[Gtk.ListBoxRow]) -> str:
        count = len(rows)
        suffix = f" ({count})" if count > 1 else ""
        return f"{base}{suffix}"

    def _popup_message_menu(
        self, row: Gtk.ListBoxRow, x: float, y: float
    ) -> None:
        rows = self._message_rows_for_menu(row)
        self._context_message_rows = rows

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
        menu.append(self._read_menu_label(rows), "win.message-toggle-read")
        menu.append(self._flag_menu_label(rows), "win.message-toggle-flag")
        if len(rows) == 1:
            menu.append("Reply", "win.message-reply")
        if can_archive:
            menu.append(
                self._count_menu_label("Archive", rows), "win.message-archive"
            )
        if can_trash:
            menu.append(
                self._count_menu_label("Move to Trash", rows), "win.message-move-trash"
            )
        self._message_popover.set_menu_model(menu)

        coords = self._message_list.translate_coordinates(
            self._message_scroll, x, y
        )
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

    def _on_message_list_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        x: float,
        y: float,
    ) -> None:
        if n_press != 1:
            return

        row = self._message_list.get_row_at_y(int(y))
        if not isinstance(row, Gtk.ListBoxRow):
            return

        event = gesture.get_current_event()
        if event is None:
            return

        if Gdk.Event.triggers_context_menu(event):
            self._popup_message_menu(row, x, y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return

        GLib.idle_add(self._update_message_toolbar)

    def _on_message_menu_toggle_read(self, *_args) -> None:
        self._toggle_message_flag("seen")

    def _on_message_menu_toggle_flag(self, *_args) -> None:
        self._toggle_message_flag("flagged")

    def _on_message_menu_archive(self, *_args) -> None:
        self._move_context_messages("archive")

    def _on_message_menu_move_trash(self, *_args) -> None:
        self._move_context_messages("trash")

    def _on_message_menu_reply(self, *_args) -> None:
        self._open_compose_reply()

    def _move_selected_messages(self, destination: str) -> None:
        rows = self._message_list.get_selected_rows()
        if not rows:
            return
        self._move_messages(destination, list(rows))

    def _move_context_messages(self, destination: str) -> None:
        self._move_messages(destination, list(self._context_message_rows))

    def _move_messages(self, destination: str, rows: list[Gtk.ListBoxRow]) -> None:
        if not rows or not self._current_account or not self._current_folder:
            return

        state = self._sidebar.get_move_menu_state(
            self._current_account.uid, self._current_folder
        )
        if destination == "archive" and not state.get("can_archive"):
            return
        if destination == "trash" and not state.get("can_trash"):
            return

        uids = [uid for row in rows if (uid := getattr(row, "message_uid", None))]
        if not uids:
            return

        account_uid = self._current_account.uid
        folder_name = self._current_folder
        self._message_popover.popdown()

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
                rows,
                destination,
                result,
                error,
            )

        label = "Trash" if destination == "trash" else "Archive"
        self._set_status(f"Moving {len(uids)} message(s) to {label}…")
        self._clear_move_undo()
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
            self._set_status(f"Undo failed: {error}")
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

    def _on_messages_moved(
        self,
        rows: list[Gtk.ListBoxRow],
        destination: str,
        result: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            self._set_status(f"Could not move messages: {error}")
            return False
        if result is None:
            return False

        removed_count = 0
        for row in list(rows):
            uid = getattr(row, "message_uid", None)
            if row.get_parent() is self._message_list:
                self._message_list.remove(row)
                removed_count += 1
            if uid == self._current_message_uid:
                self._clear_reader()

        self._shown_message_count = max(0, self._shown_message_count - removed_count)
        if self._message_total >= 0:
            self._message_total = max(0, self._message_total - removed_count)

        if removed_count == 0:
            self._set_status("Messages moved, but the list could not be updated")
            return False

        remaining_rows = 0
        child = self._message_list.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBoxRow):
                remaining_rows += 1
            child = child.get_next_sibling()
        if remaining_rows == 0 and self._current_folder:
            self._message_empty_label.set_label(
                f"No messages in {self._current_folder}"
            )
            self._message_stack.set_visible_child_name("empty")

        if self._current_account and self._current_folder:
            unread = result.get("source_folder_unread")
            total = result.get("source_folder_total")
            if unread is not None and total is not None:
                self._sidebar.update_folder_row(
                    self._current_account.uid,
                    self._current_folder,
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
                    self._current_account.uid,
                    dest_folder,
                    dest_unread,
                    dest_total,
                )

            self._update_message_status(self._current_account, self._current_folder)

            inbox_folder = self._sidebar.inbox_folder_for_account(
                self._current_account.uid
            )
            if inbox_folder and self._current_folder == inbox_folder:
                self._sidebar.refresh_inbox_counts(self._current_account.uid)

        label = "Trash" if destination == "trash" else "Archive"
        if removed_count > 1:
            status_label = f"Moved {removed_count} messages to {label}"
        else:
            status_label = f"Moved message to {label}"

        dest_folder = result.get("destination_folder")
        dest_uids = result.get("destination_uids") or []
        if (
            self._current_account
            and self._current_folder
            and dest_folder
            and dest_uids
        ):
            self._register_move_undo(
                status_label,
                account_uid=self._current_account.uid,
                source_folder=self._current_folder,
                dest_folder=dest_folder,
                dest_uids=dest_uids,
            )
            self._set_status(f"{status_label}  ·  Ctrl+Z to undo")
        else:
            self._clear_move_undo()
            self._set_status(status_label)
        self._update_message_toolbar()
        return False

    def _toggle_message_flag(self, flag_name: str) -> None:
        rows = list(self._context_message_rows)
        if not rows or not self._current_account or not self._current_folder:
            return

        uids = [uid for row in rows if (uid := getattr(row, "message_uid", None))]
        if not uids:
            return

        account_uid = self._current_account.uid
        folder_name = self._current_folder

        def worker() -> None:
            error: Exception | None = None
            result: dict | None = None
            try:
                if flag_name == "seen":
                    result = self._mail.toggle_messages_seen(
                        account_uid, folder_name, uids
                    )
                else:
                    result = self._mail.toggle_messages_flagged(
                        account_uid, folder_name, uids
                    )
            except Exception as exc:
                log.exception("Failed to toggle message %s", flag_name)
                error = exc
            GLib.idle_add(
                self._on_messages_flag_toggled,
                rows,
                flag_name,
                result,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_messages_flag_toggled(
        self,
        rows: list[Gtk.ListBoxRow],
        flag_name: str,
        result: dict | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            self._set_status(f"Could not update messages: {error}")
            return False
        if result is None:
            return False

        updates_by_uid = {
            item["uid"]: item.get("flags") or {}
            for item in result.get("updates") or []
            if item.get("uid")
        }
        for row in rows:
            uid = getattr(row, "message_uid", None)
            if not uid or uid not in updates_by_uid:
                continue
            flags = dict(getattr(row, "message_flags", {}) or {})
            flags.update(updates_by_uid[uid])
            row.message_flags = flags
            self._apply_row_flags(row, flags)

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

    def _apply_row_flags(self, row: Gtk.ListBoxRow, flags: dict) -> None:
        subject_label = getattr(row, "subject_label", None)
        if isinstance(subject_label, Gtk.Label):
            if flags.get("seen", True):
                subject_label.remove_css_class("heading")
            else:
                subject_label.add_css_class("heading")

        flag_icon = getattr(row, "flag_icon", None)
        if isinstance(flag_icon, Gtk.Image):
            flag_icon.set_visible(bool(flags.get("flagged")))

    def _on_message_list_selection_changed(
        self, _listbox: Gtk.ListBox, _row: Gtk.ListBoxRow | None
    ) -> None:
        GLib.idle_add(self._update_message_toolbar)

    def _update_message_toolbar(self) -> bool:
        rows = self._message_list.get_selected_rows()
        if len(rows) != 1:
            self._reply_btn.set_sensitive(False)
        return False

    def _on_message_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        if row is None or not self._current_account or not self._current_folder:
            return
        if len(self._message_list.get_selected_rows()) != 1:
            return
        uid = getattr(row, "message_uid", None)
        if not uid:
            return

        account = self._current_account
        folder_name = self._current_folder
        self._message_read_generation += 1
        read_id = self._message_read_generation

        def worker() -> None:
            error: Exception | None = None
            msg: dict | None = None
            try:
                msg = self._mail.read_message(account.uid, folder_name, uid)
            except Exception as exc:
                log.exception("Failed to read message")
                error = exc
            GLib.idle_add(
                self._on_message_read,
                read_id,
                row,
                uid,
                msg,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_message_read(
        self,
        read_id: int,
        row: Gtk.ListBoxRow,
        uid: str,
        msg: dict | None,
        error: Exception | None,
    ) -> bool:
        if read_id != self._message_read_generation:
            return False

        if error is not None:
            self._reader_subject.set_label("Could not read message")
            self._reader_meta.set_label(str(error))
            self._clear_attachments()
            self._current_message = None
            self._reply_btn.set_sensitive(False)
            self._current_body = {"plain": None, "html": None}
            self._web_view.load_html(
                "<body style='font-family:sans-serif;color:#666;padding:1em'>"
                "This message could not be loaded.</body>",
                None,
            )
            self._set_status(f"Read error: {error}")
            return False

        assert msg is not None
        if not self._current_account or not self._current_folder:
            return False

        self._current_message_uid = uid
        if "folder_unread" in msg and "folder_total" in msg:
            self._sidebar.update_folder_row(
                self._current_account.uid,
                self._current_folder,
                msg["folder_unread"],
                msg["folder_total"],
            )
            self._mark_row_read(row)

        self._reader_subject.set_label(msg.get("subject") or "(no subject)")
        self._reader_meta.set_label(format_message_header(msg))
        self._current_message = msg
        self._reply_btn.set_sensitive(True)
        self._show_attachments(msg.get("attachments") or [])
        self._current_body = {
            "plain": msg.get("body_plain"),
            "html": msg.get("body_html"),
        }
        self._show_reader_document()
        return False

    def _show_reader_document(self) -> None:
        document = build_reader_document(
            body_html=self._current_body.get("html"),
            body_plain=self._current_body.get("plain"),
            allow_remote=self._remote_switch.get_active(),
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
            self._set_status(f"Could not open link: {exc.message}")

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

    def _on_remote_content_changed(self, *_args) -> None:
        if self._current_body.get("html") or self._current_body.get("plain"):
            self._show_reader_document()
