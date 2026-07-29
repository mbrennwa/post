# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Separate reader window opened on message list activation."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from post.folder_dialogs import confirm_action
from post.header_bar import add_end_window_controls
from post.icon_utils import apply_window_icon
from post.mail import MailService
from post.mail.eds import MailAccount, MessageNotAvailableError
from post.mail.helpers import (
    perform_one_click_unsubscribe,
    reader_toggle_button_state,
    write_temp_attachment,
)
from post.mail.io_thread import get_mail_io_thread
from post.open_uri import open_uri_externally
from post.preferences import get_load_remote_content, get_message_appearance
from post.reader.pane import MessageReaderPane
from post.toast import show_error_toast, show_toast

log = logging.getLogger(__name__)

SetStatus = Callable[[str], None]
OnCompose = Callable[[str, dict[str, Any]], None]
OnRequestMove = Callable[[str, str, str, str], None]
OnFlagsUpdated = Callable[[str, dict[str, Any]], None]
OnMessageLoaded = Callable[[str, str, str, dict[str, Any]], None]
GetMoveState = Callable[[], dict[str, Any]]
GetMessageFlags = Callable[[str], dict[str, Any]]


class ReaderWindow(Adw.ApplicationWindow):
    """Full-featured message reader in its own window."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        account: MailAccount,
        folder_name: str,
        message_uid: str,
        set_status: SetStatus,
        on_compose: OnCompose,
        on_request_move: OnRequestMove,
        on_flags_updated: OnFlagsUpdated,
        on_message_loaded: OnMessageLoaded,
        get_move_state: GetMoveState,
        get_message_flags: GetMessageFlags,
        viewing_drafts: bool = False,
    ) -> None:
        super().__init__()
        self._parent_window = parent
        apply_window_icon(self)
        application = parent.get_application()
        if application is not None:
            self.set_application(application)

        self._mail = mail
        self._account = account
        self._folder_name = folder_name
        self._message_uid = message_uid
        self._set_status = set_status
        self._on_compose = on_compose
        self._on_request_move = on_request_move
        self._on_flags_updated = on_flags_updated
        self._on_message_loaded = on_message_loaded
        self._get_move_state = get_move_state
        self._get_message_flags = get_message_flags
        self._viewing_drafts = viewing_drafts
        self._load_remote_content = get_load_remote_content()
        self._message_appearance = get_message_appearance()
        self._read_generation = 0
        self._current_message: dict[str, Any] | None = None
        self._context_attachment_index: int | None = None
        self._context_attachment_mime: str | None = None
        self._context_attachment_name: str | None = None

        self.set_title("Loading message…")
        self.set_default_size(720, 560)

        header = Adw.HeaderBar()
        add_end_window_controls(header)

        self._read_toggle_btn = Gtk.Button(icon_name="mail-mark-read-symbolic")
        self._read_toggle_btn.set_tooltip_text("Mark as Read")
        self._read_toggle_btn.add_css_class("message-read-action")
        self._read_toggle_btn.set_sensitive(False)
        self._read_toggle_btn.connect("clicked", self._on_read_toggle)
        header.pack_start(self._read_toggle_btn)

        self._flag_toggle_btn = Gtk.Button(icon_name="mail-flag-symbolic")
        self._flag_toggle_btn.set_tooltip_text("Flag")
        self._flag_toggle_btn.add_css_class("message-flagged")
        self._flag_toggle_btn.set_sensitive(False)
        self._flag_toggle_btn.connect("clicked", self._on_flag_toggle)
        header.pack_start(self._flag_toggle_btn)

        self._archive_btn = Gtk.Button(
            icon_name="mail-archive-symbolic",
            tooltip_text="Archive",
        )
        self._archive_btn.connect("clicked", self._on_archive_clicked)
        self._archive_btn.set_sensitive(False)
        header.pack_start(self._archive_btn)

        self._trash_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            tooltip_text="Move to Trash",
        )
        self._trash_btn.connect("clicked", self._on_trash_clicked)
        self._trash_btn.set_sensitive(False)
        header.pack_start(self._trash_btn)

        self._reader_pane = MessageReaderPane(
            on_reply=self._on_reply,
            on_reply_all=self._on_reply_all,
            on_forward=self._on_forward,
            on_unsubscribe=self._on_unsubscribe,
            on_attachment_clicked=self._on_attachment_clicked,
            on_attachment_context_menu=self._on_attachment_context_menu,
            on_open_uri=self._open_uri_externally,
        )
        self._reader_pane.set_margin_start(16)
        self._reader_pane.set_margin_end(16)
        self._reader_pane.set_margin_top(12)
        self._reader_pane.set_margin_bottom(12)
        self._reader_pane.set_vexpand(True)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self._reader_pane)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)
        self._toast_overlay.set_child(toolbar_view)
        self.set_content(self._toast_overlay)

        self._setup_attachment_menu()
        self._update_move_buttons()
        self._reader_pane.show_loading()
        self._load_message()

    @property
    def account_uid(self) -> str:
        return self._account.uid

    @property
    def folder_name(self) -> str:
        return self._folder_name

    @property
    def message_uid(self) -> str:
        return self._message_uid

    @property
    def current_message(self) -> dict[str, Any] | None:
        return self._current_message

    def notify_flags_updated(self, uid: str, flags: dict[str, Any]) -> None:
        if uid != self._message_uid:
            return
        if self._current_message is not None:
            current_flags = dict(self._current_message.get("flags") or {})
            current_flags.update(flags)
            self._current_message["flags"] = current_flags
        self._reader_pane.update_message_flags(flags)
        self._refresh_flag_toggle_buttons()

    def notify_message_moved(self, uid: str) -> None:
        if uid != self._message_uid:
            return
        self.close()

    def _update_move_buttons(self) -> None:
        state = self._get_move_state()
        self._archive_btn.set_sensitive(bool(state.get("can_archive")))
        self._trash_btn.set_sensitive(bool(state.get("can_trash")))

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

    def _refresh_flag_toggle_buttons(self) -> None:
        if self._current_message is None:
            self._read_toggle_btn.set_sensitive(False)
            self._flag_toggle_btn.set_sensitive(False)
            return
        flags = self._current_message.get("flags") or {}
        toggles = reader_toggle_button_state(flags)
        self._apply_toggle_button_presentation(
            self._read_toggle_btn, toggles["read"]
        )
        self._apply_toggle_button_presentation(
            self._flag_toggle_btn, toggles["flag"]
        )
        self._read_toggle_btn.set_sensitive(True)
        self._flag_toggle_btn.set_sensitive(True)

    def _load_message(self) -> None:
        self._read_generation += 1
        read_id = self._read_generation
        account_uid = self._account.uid
        folder_name = self._folder_name
        uid = self._message_uid
        mark_seen = not self._viewing_drafts

        def worker() -> None:
            if read_id != self._read_generation:
                return
            error: Exception | None = None
            msg: dict[str, Any] | None = None
            try:
                msg = self._mail.read_message(
                    account_uid,
                    folder_name,
                    uid,
                    mark_seen=mark_seen,
                )
            except MessageNotAvailableError as exc:
                log.warning(
                    "Message %s no longer available in %r",
                    uid,
                    folder_name,
                )
                error = exc
            except Exception as exc:
                log.exception("Failed to read message in reader window")
                error = exc
            GLib.idle_add(self._on_message_read, read_id, msg, error)

        get_mail_io_thread().submit_front(worker)

    def _on_message_read(
        self,
        read_id: int,
        msg: dict[str, Any] | None,
        error: Exception | None,
    ) -> bool:
        if read_id != self._read_generation:
            return False

        if isinstance(error, MessageNotAvailableError):
            self._current_message = None
            self._reader_pane.show_unavailable(
                error.user_message(),
                dark=self._app_prefers_dark(),
            )
            self.set_title("Message unavailable")
            self._refresh_flag_toggle_buttons()
            show_error_toast(self, error.user_message())
            return False

        if error is not None:
            self._current_message = None
            self._reader_pane.show_error(error, dark=self._app_prefers_dark())
            self.set_title("Could not read message")
            self._refresh_flag_toggle_buttons()
            show_error_toast(self, f"Read error: {error}")
            return False

        assert msg is not None
        self._current_message = msg
        subject = msg.get("subject") or "(no subject)"
        self.set_title(subject)
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
        self._refresh_flag_toggle_buttons()
        self._on_message_loaded(
            self._message_uid,
            self._account.uid,
            self._folder_name,
            msg,
        )
        return False

    def _app_prefers_dark(self) -> bool:
        return Adw.StyleManager.get_default().get_dark()

    def _on_archive_clicked(self, *_args) -> None:
        self._on_request_move(
            "archive",
            self._message_uid,
            self._account.uid,
            self._folder_name,
        )

    def _on_trash_clicked(self, *_args) -> None:
        self._on_request_move(
            "trash",
            self._message_uid,
            self._account.uid,
            self._folder_name,
        )

    def _on_read_toggle(self, *_args) -> None:
        if self._current_message is None:
            return
        flags = dict(self._current_message.get("flags") or {})
        seen = not flags.get("seen", True)
        self._set_message_flag("seen", seen=seen)

    def _on_flag_toggle(self, *_args) -> None:
        if self._current_message is None:
            return
        flags = dict(self._current_message.get("flags") or {})
        flagged = not flags.get("flagged", False)
        self._set_message_flag("flagged", flagged=flagged)

    def _set_message_flag(
        self,
        flag_name: str,
        *,
        seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        uid = self._message_uid
        account_uid = self._account.uid
        folder_name = self._folder_name

        def worker() -> None:
            error: Exception | None = None
            result: dict[str, Any] | None = None
            try:
                if flag_name == "seen":
                    assert seen is not None
                    result = self._mail.set_messages_seen(
                        account_uid, folder_name, [uid], seen=seen
                    )
                else:
                    assert flagged is not None
                    result = self._mail.set_messages_flagged(
                        account_uid, folder_name, [uid], flagged=flagged
                    )
            except Exception as exc:
                log.exception("Failed to update message %s", flag_name)
                error = exc
            GLib.idle_add(
                self._on_message_flag_updated,
                flag_name,
                result,
                error,
            )

        get_mail_io_thread().submit(worker)

    def _on_message_flag_updated(
        self,
        flag_name: str,
        result: dict[str, Any] | None,
        error: Exception | None,
    ) -> bool:
        if error is not None:
            show_error_toast(self, f"Could not update message: {error}")
            return False
        if result is None:
            return False

        updates_by_uid = {
            item["uid"]: item.get("flags") or {}
            for item in result.get("updates") or []
            if item.get("uid")
        }
        flags = updates_by_uid.get(self._message_uid)
        if flags is None:
            return False

        if self._current_message is not None:
            current_flags = dict(self._current_message.get("flags") or {})
            current_flags.update(flags)
            self._current_message["flags"] = current_flags
        self._reader_pane.update_message_flags(flags)
        self._refresh_flag_toggle_buttons()
        self._on_flags_updated(self._message_uid, flags)
        return False

    def _on_reply(self, *_args) -> None:
        self._open_compose("reply")

    def _on_reply_all(self, *_args) -> None:
        self._open_compose("reply-all")

    def _on_forward(self, *_args) -> None:
        self._open_compose("forward")

    def _on_unsubscribe(self, action: dict[str, str]) -> None:
        kind = action.get("kind")
        url = action.get("url")
        if kind not in ("post", "open") or not isinstance(url, str) or not url:
            return
        if kind == "open":
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
            self._archive_after_unsubscribe()
            return
        will_archive = bool(self._get_move_state().get("can_archive"))
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
            GLib.idle_add(self._on_one_click_unsubscribe_done, error)

        get_mail_io_thread().submit(worker)

    def _archive_after_unsubscribe(self) -> None:
        if not self._get_move_state().get("can_archive"):
            return
        self._on_request_move(
            "archive",
            self._message_uid,
            self._account.uid,
            self._folder_name,
        )

    def _on_one_click_unsubscribe_done(self, error: Exception | None) -> bool:
        if error is not None:
            show_error_toast(self, f"Unsubscribe failed: {error}")
        else:
            show_toast(self, "Unsubscribe request sent")
            self._archive_after_unsubscribe()
        return False

    def _open_compose(self, mode: str) -> None:
        if self._current_message is None:
            return
        self._on_compose(mode, self._current_message)

    def _setup_attachment_menu(self) -> None:
        menu = Gio.Menu()
        menu.append("Save As…", "win.save-attachment")
        menu.append("Open With…", "win.open-with-attachment")

        popover = Gtk.PopoverMenu()
        popover.set_menu_model(menu)
        self._attachment_popover = popover

        save_action = Gio.SimpleAction.new("save-attachment", None)
        save_action.connect("activate", self._on_attachment_menu_save)
        self.add_action(save_action)

        open_with_action = Gio.SimpleAction.new("open-with-attachment", None)
        open_with_action.connect("activate", self._on_attachment_menu_open_with)
        self.add_action(open_with_action)

    def _on_attachment_context_menu(
        self,
        widget: Gtk.Widget,
        x: float,
        y: float,
        index: int,
        mime_type: str | None,
        name: str,
    ) -> None:
        self._context_attachment_index = index
        self._context_attachment_mime = mime_type
        self._context_attachment_name = name
        self._attachment_popover.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        self._attachment_popover.set_pointing_to(rect)
        self._attachment_popover.popup()

    def _on_attachment_clicked(self, attachment_index: int) -> None:
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

    def _fetch_attachment(
        self,
        attachment_index: int,
        on_ready: Callable[[str, bytes | None, Exception | None], None],
    ) -> None:
        account_uid = self._account.uid
        folder_name = self._folder_name
        message_uid = self._message_uid

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
            GLib.idle_add(self._on_attachment_fetched, filename, data, error, on_ready)

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
            show_error_toast(self, f"Could not open attachment: {error}")
            return
        if data is None:
            return
        path = write_temp_attachment(filename, data)
        try:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(path, None),
                None,
            )
        except GLib.Error as exc:
            show_error_toast(self, f"Could not open attachment: {exc.message}")

    def _prompt_save_attachment(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Could not save attachment: {error}")
            return
        if data is None:
            return

        dialog = Gtk.FileDialog(title="Save Attachment")
        dialog.set_initial_name(filename)

        def on_response(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = _dialog.save_finish(result)
            except GLib.Error as exc:
                if exc.matches(Gtk.dialog_error_quark(), int(Gtk.DialogError.DISMISSED)):
                    return
                show_error_toast(self, f"Could not save attachment: {exc.message}")
                return
            path = file.get_path()
            if path is None:
                return
            try:
                with open(path, "wb") as handle:
                    handle.write(data)
                show_toast(self, f"Saved {os.path.basename(path)}")
            except OSError as exc:
                show_error_toast(self, f"Could not save attachment: {exc}")

        dialog.save(self, None, on_response)

    def _prompt_open_with_dialog(
        self,
        filename: str,
        data: bytes | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            show_error_toast(self, f"Could not open attachment: {error}")
            return
        if data is None:
            return
        path = write_temp_attachment(filename, data)
        uri = GLib.filename_to_uri(path, None)
        dialog = Gtk.AppChooserDialog(
            transient_for=self,
            modal=True,
            heading=f"Open {filename}",
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_gicon(Gio.FileIcon.new(Gio.File.new_for_uri(uri)))

        def on_response(_dialog: Gtk.AppChooserDialog, response: int) -> None:
            if response != Gtk.ResponseType.OK:
                return
            app_info = _dialog.get_app_info()
            if app_info is None:
                return
            try:
                app_info.launch([Gio.File.new_for_uri(uri)], None)
            except GLib.Error as exc:
                show_error_toast(self, f"Could not open attachment: {exc.message}")

        dialog.connect("response", on_response)
        dialog.present()

    def _open_uri_externally(self, uri: str) -> None:
        open_uri_externally(
            self,
            uri,
            on_error=lambda message: show_error_toast(self, message),
        )
