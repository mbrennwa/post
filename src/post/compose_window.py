# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compose window for new messages and replies."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk

from post.header_bar import add_end_window_controls, apply_header_corner_inset
from post.icon_utils import apply_window_icon
from post.mail import MailService
from post.mail.mailto import MailtoCompose
from post.mail.compose import (
    ComposeAttachment,
    body_mentions_attachment,
    body_html_for_quoting,
    body_text_for_quoting,
    build_forward_subject,
    build_outbound_html_for_compose,
    build_reply_all_recipients,
    build_reply_references,
    normalize_in_reply_to,
    normalize_references_header,
    build_reply_subject,
    compose_body_with_signature,
    extract_user_body_from_auto_signature,
    extract_reply_target_addresses,
    find_auto_signature_offset,
    finalize_body_after_signature_sync,
    format_address_list,
    guess_attachment_mime_type,
    normalize_signature_text,
    normalize_email,
    parse_address_list,
    parse_draft_address_list,
    is_plain_wrapper_html,
    quote_plain_forward,
    quote_plain_reply,
    replace_new_message_signature,
    validate_compose_mime_fields,
)
from post.mail.helpers import format_attachment_size, write_temp_attachment
from post.mail.correspondents import (
    Correspondent,
    apply_address_completion,
    current_address_token,
    match_correspondents,
)
from post.mail.eds import MailAccount
from post.mail.io_thread import get_mail_io_thread
from post.mail.send_errors import (
    SendQueued,
    is_compose_validation_error,
    user_send_error_message,
)
from post.mail.draft_queue import is_queued_draft_id
from post.mail.send_queue import (
    load_queued_attachments,
    load_queued_outbound_message,
    new_outbound_queue_id,
    persist_outbound_send,
    remove_queued_outbound_message,
)
from post.preferences import (
    format_send_delay_status,
    get_account_signature,
    get_account_signatures,
    get_send_delay_seconds,
)
from post.toast import show_error_toast, show_toast

log = logging.getLogger(__name__)

ComposeMode = Literal["new", "reply", "reply-all", "forward", "draft", "outbox", "send-again"]
SetStatus = Callable[[str], None]


@dataclass(frozen=True)
class SavedDraftNotification:
    account_uid: str
    folder_name: str
    uid: str | None
    previous_uid: str | None
    subject: str
    to: str
    from_label: str
    has_attachments: bool
    sort_date: float
    removed: bool = False


OnDraftSaved = Callable[[SavedDraftNotification], None]
OnDraftSaveStarted = Callable[[str, str | None], None]


@dataclass
class OutboundSendRequest:
    account_uid: str
    to: list[str]
    cc: list[str] | None
    bcc: list[str] | None
    subject: str
    body: str
    body_html: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    attachments: list[ComposeAttachment] | None = None
    draft_folder: str | None = None
    draft_uid: str | None = None
    queue_id: str | None = None
    send_immediately: bool = False


_OUTBOX_FAILURE_SUFFIX = " Message saved in Outbox."


def run_outbound_send(
    *,
    mail: MailService,
    parent: Gtk.Window | None,
    set_status: SetStatus,
    on_outbox_changed: Callable[[], None] | None,
    on_draft_saved: OnDraftSaved | None,
    on_delayed_send: Callable[[str, float], None] | None = None,
    request: OutboundSendRequest,
) -> None:
    """Persist to outbox and send on the mail I/O thread; never block the UI thread."""

    get_mail_io_thread().submit(
        _run_outbound_send_worker,
        mail=mail,
        parent=parent,
        set_status=set_status,
        on_outbox_changed=on_outbox_changed,
        on_draft_saved=on_draft_saved,
        on_delayed_send=on_delayed_send,
        request=request,
    )


def _run_outbound_send_worker(
    *,
    mail: MailService,
    parent: Gtk.Window | None,
    set_status: SetStatus,
    on_outbox_changed: Callable[[], None] | None,
    on_draft_saved: OnDraftSaved | None,
    on_delayed_send: Callable[[str, float], None] | None,
    request: OutboundSendRequest,
) -> None:
    queue_id = request.queue_id or new_outbound_queue_id()
    mail.begin_outbound_send()
    try:
        mail.claim_outbound_delivery(queue_id)
        try:
            account = mail.get_account(request.account_uid)
            validate_compose_mime_fields(
                from_name=account.from_name,
                subject=request.subject,
                to=request.to,
                cc=request.cc,
                bcc=request.bcc,
                in_reply_to=request.in_reply_to,
                references=request.references,
                attachments=request.attachments,
            )
            delay_seconds = (
                0 if request.send_immediately else get_send_delay_seconds()
            )
            send_after = (
                time.time() + delay_seconds if delay_seconds > 0 else None
            )
            persist_outbound_send(
                account_uid=request.account_uid,
                to=request.to,
                cc=request.cc,
                bcc=request.bcc,
                subject=request.subject,
                body=request.body,
                body_html=request.body_html,
                in_reply_to=request.in_reply_to,
                references=request.references,
                attachments=request.attachments,
                queue_id=queue_id,
                send_after=send_after,
            )
        except ValueError as exc:
            log.warning("Outbound compose validation failed: %s", exc)
            if parent is not None:
                GLib.idle_add(
                    _show_send_error_toast,
                    parent,
                    user_send_error_message(exc),
                )
            return
        except OSError as exc:
            log.error("Could not persist outbound message to outbox: %s", exc)
            if parent is not None:
                GLib.idle_add(
                    _show_send_error_toast,
                    parent,
                    f"Could not save message for sending: {exc}",
                )
            return

        request_with_id = replace(request, queue_id=queue_id)
        if on_outbox_changed is not None:
            GLib.idle_add(_notify_outbox_changed, on_outbox_changed)

        if delay_seconds > 0 and send_after is not None:
            if on_delayed_send is not None:
                GLib.idle_add(_schedule_delayed_send, on_delayed_send, queue_id, send_after)
            GLib.idle_add(
                _finish_outbound_send,
                parent,
                set_status,
                on_draft_saved,
                on_outbox_changed,
                mail,
                request_with_id,
                None,
                format_send_delay_status(delay_seconds),
            )
            return

        log.debug("Starting SMTP delivery for outbox item %s", queue_id)

        try:
            mail.deliver_outbound_queue_item(queue_id)
        except SendQueued as exc:
            if on_outbox_changed is not None:
                GLib.idle_add(_notify_outbox_changed, on_outbox_changed)
            GLib.idle_add(
                _finish_outbound_send,
                parent,
                set_status,
                on_draft_saved,
                on_outbox_changed,
                mail,
                request_with_id,
                None,
                exc.user_message,
            )
            return
        except Exception as exc:
            log.warning("Send failed: %s", user_send_error_message(exc), exc_info=True)
            GLib.idle_add(
                _finish_outbound_send,
                parent,
                set_status,
                on_draft_saved,
                on_outbox_changed,
                mail,
                request_with_id,
                exc,
                None,
            )
            return

        GLib.idle_add(
            _finish_outbound_send,
            parent,
            set_status,
            on_draft_saved,
            on_outbox_changed,
            mail,
            request_with_id,
            None,
            None,
        )
    finally:
        mail.end_outbound_send()
        mail.release_outbound_delivery(queue_id)


def _show_send_error_toast(parent: Gtk.Window, message: str) -> bool:
    show_error_toast(parent, message)
    return False


def _notify_outbox_changed(on_outbox_changed: Callable[[], None]) -> bool:
    on_outbox_changed()
    return False


def _schedule_delayed_send(
    on_delayed_send: Callable[[str, float], None],
    queue_id: str,
    send_after: float,
) -> bool:
    on_delayed_send(queue_id, send_after)
    return False


def _finish_outbound_send(
    parent: Gtk.Window | None,
    set_status: SetStatus,
    on_draft_saved: OnDraftSaved | None,
    on_outbox_changed: Callable[[], None] | None,
    mail: MailService,
    request: OutboundSendRequest,
    error: Exception | None,
    success_status: str | None,
) -> bool:
    if error is not None:
        message = user_send_error_message(error)
        if request.queue_id:
            if is_compose_validation_error(error):
                remove_queued_outbound_message(request.queue_id)
            else:
                message = f"{message}{_OUTBOX_FAILURE_SUFFIX}"
        if parent is not None:
            show_error_toast(parent, message)
        else:
            log.error("Send failed (no parent window for toast): %s", message)
        if on_outbox_changed is not None:
            on_outbox_changed()
        return False

    if request.draft_folder and request.draft_uid:

        def delete_worker() -> None:
            try:
                mail.delete_draft(
                    request.account_uid,
                    request.draft_folder,
                    request.draft_uid,
                )
            except Exception:
                log.warning(
                    "Could not delete draft %s in %r after send",
                    request.draft_uid,
                    request.draft_folder,
                    exc_info=True,
                )
            GLib.idle_add(
                _complete_outbound_send_success,
                set_status,
                on_draft_saved,
                on_outbox_changed,
                success_status,
                request,
            )

        get_mail_io_thread().submit(delete_worker)
        return False

    _complete_outbound_send_success(
        set_status, on_draft_saved, on_outbox_changed, success_status, request
    )
    return False


def _complete_outbound_send_success(
    set_status: SetStatus,
    on_draft_saved: OnDraftSaved | None,
    on_outbox_changed: Callable[[], None] | None,
    success_status: str | None,
    request: OutboundSendRequest,
) -> bool:
    if on_draft_saved is not None and request.draft_folder and request.draft_uid:
        on_draft_saved(
            SavedDraftNotification(
                account_uid=request.account_uid,
                folder_name=request.draft_folder,
                uid=request.draft_uid,
                previous_uid=request.draft_uid,
                subject="",
                to="",
                from_label="",
                has_attachments=False,
                sort_date=0.0,
                removed=True,
            )
        )
    if on_outbox_changed is not None:
        on_outbox_changed()
    set_status(success_status or "Message sent")
    return False


_LABEL_WIDTH = 72
_TO_PLACEHOLDER = "Add a recipient in the To field"
_CC_PLACEHOLDER = "Optional"
_BCC_PLACEHOLDER = "Optional"
_SUBJECT_PLACEHOLDER = "Subject is required"
_SIGNATURE_MARK_NAME = "compose-auto-signature"


class ComposeWindow(Adw.Window):
    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        account: MailAccount,
        set_status: SetStatus,
        on_outbox_changed: Callable[[], None] | None = None,
        on_draft_saved: OnDraftSaved | None = None,
        on_draft_save_started: OnDraftSaveStarted | None = None,
        on_delayed_send: Callable[[str, float], None] | None = None,
        mode: ComposeMode = "new",
        reply_to: dict[str, Any] | None = None,
        draft_folder_name: str | None = None,
        draft_message_uid: str | None = None,
        draft_message: dict[str, Any] | None = None,
        source_folder_name: str | None = None,
        source_message_uid: str | None = None,
        outbox_queue_id: str | None = None,
        mailto: MailtoCompose | None = None,
    ) -> None:
        super().__init__()
        self._parent_window = parent
        apply_window_icon(self)
        if parent is not None:
            application = parent.get_application()
            if application is not None:
                self.set_application(application)
        self._mail = mail
        self._set_status = set_status
        self._on_outbox_changed = on_outbox_changed
        self._on_draft_saved = on_draft_saved
        self._on_draft_save_started = on_draft_save_started
        self._on_delayed_send = on_delayed_send
        self._mode = mode
        self._reply_to = reply_to
        self._draft_folder_name = draft_folder_name
        self._draft_message_uid = draft_message_uid
        self._draft_message = draft_message
        self._source_folder_name = source_folder_name
        self._source_message_uid = source_message_uid
        self._outbox_queue_id = outbox_queue_id
        self._mailto = mailto
        self._saving_draft = False
        self._draft_save_generation = 0
        self._draft_save_cancellable: Gio.Cancellable | None = None
        self._draft_save_timeout_id: int | None = None
        self._close_while_saving_dialog: Adw.AlertDialog | None = None
        self._attachments: list[ComposeAttachment] = []
        self._close_when_saved = False
        self._unsaved_dialog: Adw.AlertDialog | None = None
        self._missing_attachment_dialog: Adw.AlertDialog | None = None
        self._user_edited = False
        self._force_close = False
        self._tracking_edits = False
        self._tracked_signature: str | None = None
        self._previous_from_account_uid = account.uid
        self._quoted_html_source: str | None = None
        self._quoted_plain_expected = ""
        self._draft_body_html: str | None = None
        self._draft_body_plain_snapshot = ""
        self._pending_draft_body = ""
        self._pending_draft_body_html: str | None = None
        self._send_accounts = mail.list_sendable_accounts()
        if not self._send_accounts and not (account.from_address or account.email):
            raise ValueError("No mail account configured for sending")
        self._from_accounts = MailService.compose_from_accounts(
            self._send_accounts, account
        )
        self._account = account

        if mode == "reply-all":
            title = "Reply All"
        elif mode == "reply":
            title = "Reply"
        elif mode == "forward":
            title = "Forward"
        elif mode == "draft":
            title = "Draft"
        elif mode == "outbox":
            title = "Edit Queued Message"
        elif mode == "send-again":
            title = "Send Again"
        else:
            title = "New Message"
        self.set_title(title)
        self.set_default_size(720, 560)

        header = Adw.HeaderBar()
        header.set_show_title(False)
        add_end_window_controls(header)
        apply_header_corner_inset(header)

        self._save_draft_btn = Gtk.Button(label="Save Draft")
        self._save_draft_btn.connect("clicked", self._on_save_draft_clicked)

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.connect("clicked", self._on_send_clicked)

        self._attach_files_btn = Gtk.Button(label="Attach Files")
        self._attach_files_btn.connect("clicked", self._on_attach_clicked)

        send_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        send_actions.add_css_class("linked")
        send_actions.append(self._send_btn)
        send_actions.append(self._save_draft_btn)
        send_actions.append(self._attach_files_btn)
        header.pack_start(send_actions)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Headers stay outside the body scroller so focusing/editing the body
        # cannot drag the whole form (#167 / #149).
        headers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        headers.set_margin_start(18)
        headers.set_margin_end(18)
        headers.set_margin_top(12)
        headers.set_margin_bottom(12)

        from_labels = [from_account.from_label for from_account in self._from_accounts]
        self._from_dropdown = Gtk.DropDown.new_from_strings(from_labels)
        self._from_dropdown.set_hexpand(True)
        for index, from_account in enumerate(self._from_accounts):
            if from_account.uid == self._account.uid:
                self._from_dropdown.set_selected(index)
                break
        if len(self._from_accounts) == 1:
            self._from_dropdown.set_sensitive(False)
        self._from_dropdown.connect("notify::selected", self._on_from_account_changed)
        headers.append(self._labeled_row("From", self._from_dropdown))

        self._correspondents: list[Correspondent] = []
        self._correspondents_generation = 0
        self._completion_model = Gtk.ListStore(str)
        self._entry_match_cache: dict[int, tuple[str, int, frozenset[str]]] = {}

        self._to_entry = Gtk.Entry()
        self._to_entry.set_placeholder_text(_TO_PLACEHOLDER)
        self._to_entry.set_hexpand(True)
        self._to_entry.connect("changed", self._on_form_field_changed)
        self._setup_address_completion(self._to_entry)

        self._cc_toggle_btn = Gtk.Button(label="Cc")
        self._cc_toggle_btn.add_css_class("flat")
        self._cc_toggle_btn.set_can_focus(False)
        self._cc_toggle_btn.connect("clicked", self._on_toggle_cc)

        self._bcc_toggle_btn = Gtk.Button(label="Bcc")
        self._bcc_toggle_btn.add_css_class("flat")
        self._bcc_toggle_btn.set_can_focus(False)
        self._bcc_toggle_btn.connect("clicked", self._on_toggle_bcc)

        self._to_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._to_row.append(self._field_label("To"))
        self._to_row.append(self._to_entry)
        self._to_row.append(self._cc_toggle_btn)
        self._to_row.append(self._bcc_toggle_btn)
        headers.append(self._to_row)

        self._cc_entry = Gtk.Entry()
        self._cc_entry.set_placeholder_text(_CC_PLACEHOLDER)
        self._cc_entry.set_hexpand(True)
        self._cc_entry.set_can_focus(False)
        self._cc_entry.connect("changed", self._on_form_field_changed)
        self._cc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._cc_row.append(self._field_label("Cc"))
        self._cc_row.append(self._cc_entry)
        self._cc_row.set_visible(False)
        headers.append(self._cc_row)
        self._setup_address_completion(self._cc_entry)

        self._bcc_entry = Gtk.Entry()
        self._bcc_entry.set_placeholder_text(_BCC_PLACEHOLDER)
        self._bcc_entry.set_hexpand(True)
        self._bcc_entry.set_can_focus(False)
        self._bcc_entry.connect("changed", self._on_form_field_changed)
        self._bcc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._bcc_row.append(self._field_label("Bcc"))
        self._bcc_row.append(self._bcc_entry)
        self._bcc_row.set_visible(False)
        headers.append(self._bcc_row)
        self._setup_address_completion(self._bcc_entry)

        self._subject_entry = Gtk.Entry()
        self._subject_entry.set_placeholder_text(_SUBJECT_PLACEHOLDER)
        self._subject_entry.set_hexpand(True)
        self._subject_entry.connect("changed", self._on_form_field_changed)
        subject_focus = Gtk.EventControllerFocus()
        subject_focus.connect("leave", self._on_subject_focus_leave)
        self._subject_entry.add_controller(subject_focus)
        headers.append(self._labeled_row("Subject", self._subject_entry))

        self._attachments_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._attachments_box.set_visible(False)
        headers.append(self._attachments_box)

        content.append(headers)

        self._body_scrolled = Gtk.ScrolledWindow()
        self._body_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._body_scrolled.set_vexpand(True)
        self._body_scrolled.set_propagate_natural_height(False)

        self._body_view = Gtk.TextView()
        self._body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._body_view.set_left_margin(10)
        self._body_view.set_right_margin(10)
        self._body_view.set_top_margin(10)
        self._body_view.set_bottom_margin(10)
        self._body_view.get_buffer().connect("changed", self._on_body_buffer_changed)
        body_focus = Gtk.EventControllerFocus()
        body_focus.connect("enter", self._on_body_focus_in)
        self._body_view.add_controller(body_focus)
        self._body_scrolled.set_child(self._body_view)
        content.append(self._body_scrolled)

        self._focus_body_at_start_on_enter = False

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(content)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar_view)
        self.set_content(self._toast_overlay)

        self._prefill_fields()
        self._tracking_edits = True
        self._load_correspondents()
        self._update_field_hints()
        self._update_send_enabled()
        self._update_save_draft_enabled()
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._set_initial_focus)
        if self._mode in ("draft", "send-again"):
            GLib.idle_add(self._begin_load_draft_attachments)
        elif self._mode == "forward":
            GLib.idle_add(self._begin_load_forward_attachments)

    def _on_attach_clicked(self, *_args) -> None:
        if self._saving_draft:
            return
        dialog = Gtk.FileDialog(title="Attach Files")

        def on_selected(_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                files = _dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            if not files:
                return
            added = False
            for gfile in files:
                path = gfile.get_path()
                if not path:
                    continue
                filename = os.path.basename(path)
                try:
                    with open(path, "rb") as handle:
                        data = handle.read()
                except OSError as exc:
                    show_error_toast(self, f"Could not read {filename}: {exc}")
                    continue
                mime_type = guess_attachment_mime_type(filename, data)
                self._attachments.append(
                    ComposeAttachment(
                        filename=filename,
                        mime_type=mime_type,
                        data=data,
                    )
                )
                added = True
            if added:
                self._mark_user_edited()
                self._refresh_attachments_ui()

        dialog.open_multiple(self, None, on_selected)

    def _refresh_attachments_ui(self) -> None:
        while child := self._attachments_box.get_first_child():
            self._attachments_box.remove(child)
        if not self._attachments:
            self._attachments_box.set_visible(False)
            return
        self._attachments_box.set_visible(True)
        for index, attachment in enumerate(self._attachments):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

            open_btn = Gtk.Button()
            open_btn.add_css_class("flat")
            open_btn.set_tooltip_text("Open Attachment")
            open_btn.set_hexpand(True)
            open_btn.set_halign(Gtk.Align.FILL)
            open_btn.connect("clicked", self._on_open_attachment_clicked, index)

            open_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
            icon.add_css_class("dim-label")
            open_content.append(icon)
            label = Gtk.Label(
                label=f"{attachment.filename} ({format_attachment_size(len(attachment.data))})"
            )
            label.set_xalign(0)
            label.set_hexpand(True)
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
            open_content.append(label)
            open_btn.set_child(open_content)
            row.append(open_btn)

            remove_btn = Gtk.Button(icon_name="window-close-symbolic")
            remove_btn.set_tooltip_text("Remove Attachment")
            remove_btn.add_css_class("flat")
            remove_btn.connect("clicked", self._on_remove_attachment, index)
            row.append(remove_btn)
            self._attachments_box.append(row)

    def _on_open_attachment_clicked(self, _button: Gtk.Button, index: int) -> None:
        if self._saving_draft:
            return
        if index < 0 or index >= len(self._attachments):
            return
        attachment = self._attachments[index]
        try:
            path = write_temp_attachment(attachment.filename, attachment.data)
            file = Gio.File.new_for_path(path)
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except (OSError, GLib.Error) as exc:
            show_error_toast(self, f"Could not open attachment: {exc}")
            return
        self._set_status(f"Opened {os.path.basename(attachment.filename)}")

    def _on_remove_attachment(self, _button: Gtk.Button, index: int) -> None:
        if self._saving_draft:
            return
        if index < 0 or index >= len(self._attachments):
            return
        del self._attachments[index]
        self._mark_user_edited()
        self._refresh_attachments_ui()

    def _begin_load_draft_attachments(self) -> bool:
        if self._mode not in ("draft", "send-again") or self._draft_message is None:
            return False
        attachments_meta = self._draft_message.get("attachments") or []
        if not attachments_meta:
            return False
        folder_name = self._draft_folder_name
        message_uid = self._draft_message_uid
        if not folder_name or not message_uid:
            return False

        account_uid = self._account.uid

        def worker() -> None:
            loaded: list[ComposeAttachment] = []
            for meta in attachments_meta:
                attachment_index = meta.get("index")
                if attachment_index is None:
                    continue
                try:
                    filename, data = self._mail.read_attachment_data(
                        account_uid,
                        folder_name,
                        message_uid,
                        int(attachment_index),
                    )
                except Exception as exc:
                    log.warning(
                        "Could not load draft attachment %s: %s",
                        attachment_index,
                        exc,
                    )
                    continue
                mime_type = str(
                    meta.get("mime_type")
                    or guess_attachment_mime_type(filename, data)
                )
                loaded.append(
                    ComposeAttachment(
                        filename=filename,
                        mime_type=mime_type,
                        data=data,
                    )
                )
            GLib.idle_add(self._on_draft_attachments_loaded, loaded)

        get_mail_io_thread().submit(worker)
        return False

    def _begin_load_forward_attachments(self) -> bool:
        if self._mode != "forward" or self._reply_to is None:
            return False
        attachments_meta = self._reply_to.get("attachments") or []
        if not attachments_meta:
            return False
        folder_name = self._source_folder_name
        message_uid = self._source_message_uid
        if not folder_name or not message_uid:
            return False

        account_uid = self._account.uid

        def worker() -> None:
            try:
                loaded = self._mail.read_compose_attachments(
                    account_uid,
                    folder_name,
                    message_uid,
                )
            except Exception as exc:
                log.warning(
                    "Could not load forward attachments for %s/%s: %s",
                    folder_name,
                    message_uid,
                    exc,
                )
                loaded = []
            GLib.idle_add(self._on_draft_attachments_loaded, loaded)

        get_mail_io_thread().submit(worker)
        return False

    def _on_draft_attachments_loaded(
        self, attachments: list[ComposeAttachment]
    ) -> bool:
        if attachments:
            self._attachments = attachments
            self._refresh_attachments_ui()
        return False

    def _set_initial_focus(self) -> bool:
        if self._mode == "new":
            self._to_entry.grab_focus()
            return False
        if self._mode in (
            "draft",
            "send-again",
            "reply",
            "reply-all",
            "forward",
            "outbox",
        ):
            # Place the caret first: grab_focus with insert at EOF scrolls the
            # body ScrolledWindow to the bottom (#149).
            self._focus_body_at_start()
        return False

    def _focus_body_at_start(self) -> None:
        self._place_body_cursor_at_start()
        self._body_view.grab_focus()
        self._scroll_body_to_top()
        # Focus may schedule a scroll-to-cursor after this handler returns.
        GLib.idle_add(self._scroll_body_to_top_idle)

    def _scroll_body_to_top(self) -> None:
        adj = self._body_scrolled.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_lower())

    def _scroll_body_to_top_idle(self) -> bool:
        self._scroll_body_to_top()
        return False

    def _setup_address_completion(self, entry: Gtk.Entry) -> None:
        completion = Gtk.EntryCompletion()
        completion.set_model(self._completion_model)
        completion.set_text_column(0)
        completion.set_minimum_key_length(1)
        completion.set_popup_completion(True)

        def match_func(completion, key, tree_iter, entry) -> bool:
            token = current_address_token(key)
            if not token:
                return False
            entry_id = id(entry)
            cached = self._entry_match_cache.get(entry_id)
            if (
                cached is None
                or cached[0] != token
                or cached[1] != self._correspondents_generation
            ):
                matches = match_correspondents(self._correspondents, token, limit=100)
                cached = (
                    token,
                    self._correspondents_generation,
                    frozenset(match.display for match in matches),
                )
                self._entry_match_cache[entry_id] = cached
            display = completion.get_model().get_value(tree_iter, 0)
            return display in cached[2]

        completion.set_match_func(match_func, entry)
        entry.connect("changed", self._on_address_entry_changed, entry)

        def on_match_selected(completion, model, tree_iter, entry) -> bool:
            display = model.get_value(tree_iter, 0)
            entry.set_text(apply_address_completion(entry.get_text(), display))
            entry.set_position(-1)
            self._entry_match_cache.pop(id(entry), None)
            return True

        completion.connect("match-selected", on_match_selected, entry)
        entry.set_completion(completion)

    def _on_address_entry_changed(self, entry: Gtk.Entry, *_args) -> None:
        self._entry_match_cache.pop(id(entry), None)

    def _refresh_address_completions(self) -> None:
        self._entry_match_cache.clear()
        for entry in (self._to_entry, self._cc_entry, self._bcc_entry):
            completion = entry.get_completion()
            if completion is None:
                continue
            if current_address_token(entry.get_text()):
                completion.complete()

    def _set_completion_model(self, model: Gtk.ListStore) -> None:
        self._completion_model = model
        self._entry_match_cache.clear()
        for entry in (self._to_entry, self._cc_entry, self._bcc_entry):
            completion = entry.get_completion()
            if completion is not None:
                completion.set_model(model)

    def _on_from_account_changed(self, *_args) -> None:
        if self._mode == "new":
            previous_signature = get_account_signature(self._previous_from_account_uid)
            self._sync_signature_for_account(previous_signature=previous_signature)
            self._previous_from_account_uid = self._selected_account().uid
        else:
            self._mark_user_edited()
        self._load_correspondents()
        self._update_send_enabled()

    def _set_body_plain_text(self, text: str) -> None:
        self._tracking_edits = False
        self._body_view.get_buffer().set_text(text)
        self._tracking_edits = True

    def _known_signatures(self) -> list[str]:
        return list(get_account_signatures().values())

    def _clear_signature_mark(self) -> None:
        buffer = self._body_view.get_buffer()
        mark = buffer.get_mark(_SIGNATURE_MARK_NAME)
        if mark is not None:
            buffer.delete_mark(mark)

    def _place_signature_mark(self) -> None:
        if self._mode != "new" or self._tracked_signature is None:
            return
        buffer = self._body_view.get_buffer()
        text = buffer.get_text(*buffer.get_bounds(), False)
        offset = find_auto_signature_offset(
            text,
            tracked_signature=self._tracked_signature,
            known_signatures=self._known_signatures(),
        )
        if offset is None:
            return
        mark_iter = buffer.get_iter_at_offset(offset)
        mark = buffer.get_mark(_SIGNATURE_MARK_NAME)
        if mark is None:
            buffer.create_mark(_SIGNATURE_MARK_NAME, mark_iter, True)
        else:
            buffer.move_mark(mark, mark_iter)

    def _sync_signature_for_account(
        self, *, previous_signature: str | None = None
    ) -> None:
        if self._mode != "new":
            return
        buffer = self._body_view.get_buffer()
        current = buffer.get_text(*buffer.get_bounds(), False)
        new_signature = get_account_signature(self._selected_account().uid)

        result = replace_new_message_signature(
            current,
            new_signature=new_signature,
            tracked_signature=self._tracked_signature,
            previous_signature=previous_signature,
            known_signatures=self._known_signatures(),
        )
        if result is None:
            user = extract_user_body_from_auto_signature(
                current,
                tracked_signature=self._tracked_signature,
                previous_signature=previous_signature,
                known_signatures=self._known_signatures(),
            )
            if user is not None:
                normalized = normalize_signature_text(new_signature)
                result = (
                    finalize_body_after_signature_sync(
                        merge_user_body_with_signature(user, normalized),
                        new_signature,
                    ),
                    normalized or None,
                )

        self._tracking_edits = False
        try:
            if result is None:
                self._clear_signature_mark()
                self._tracked_signature = None
                return
            new_body, new_tracked = result
            self._set_body_plain_text(new_body)
            self._tracked_signature = new_tracked
            if new_tracked:
                self._place_signature_mark()
            else:
                self._clear_signature_mark()
            if not new_body.strip():
                self._place_body_cursor_at_start()
        finally:
            self._tracking_edits = True

    def _on_body_buffer_changed(self, *_args) -> None:
        if not self._tracking_edits:
            return
        if self._mode == "new" and self._tracked_signature is not None:
            buffer = self._body_view.get_buffer()
            text = buffer.get_text(*buffer.get_bounds(), False)
            if (
                find_auto_signature_offset(
                    text,
                    tracked_signature=self._tracked_signature,
                    known_signatures=self._known_signatures(),
                )
                is None
            ):
                self._tracked_signature = None
                self._clear_signature_mark()
            else:
                self._place_signature_mark()
        elif self._mode == "new":
            self._clear_signature_mark()
        self._mark_user_edited()

    def _load_correspondents(self) -> None:
        account = self._selected_account()
        generation = self._correspondents_generation + 1
        self._correspondents_generation = generation

        def worker() -> None:
            try:
                correspondents = self._mail.get_correspondents(account.uid)
            except Exception:
                log.warning(
                    "Could not load correspondents for %s",
                    account.uid,
                    exc_info=True,
                )
                return
            GLib.idle_add(
                self._on_correspondents_loaded, generation, correspondents
            )

        get_mail_io_thread().submit(worker)

    def _on_correspondents_loaded(
        self, generation: int, correspondents: list[Correspondent]
    ) -> bool:
        if generation != self._correspondents_generation:
            return False
        self._correspondents = correspondents
        model = Gtk.ListStore(str)
        for item in correspondents:
            model.append([item.display])
        self._set_completion_model(model)
        self._refresh_address_completions()
        return False

    @staticmethod
    def _field_label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_width_chars(8)
        label.set_xalign(0)
        return label

    @classmethod
    def _labeled_row(cls, label: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(cls._field_label(label))
        if isinstance(widget, Gtk.Entry):
            widget.set_hexpand(True)
        elif isinstance(widget, Gtk.DropDown):
            widget.set_hexpand(True)
        elif isinstance(widget, Gtk.Label):
            widget.set_hexpand(True)
            widget.set_halign(Gtk.Align.START)
            widget.set_valign(Gtk.Align.CENTER)
        row.append(widget)
        return row

    def _selected_account(self) -> MailAccount:
        index = self._from_dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return self._account
        return self._from_accounts[index]

    def _own_addresses(self) -> set[str]:
        account = self._selected_account()
        own: set[str] = set()
        for raw in (account.from_address, account.email):
            if raw:
                own.add(normalize_email(raw))
        return own

    def _mark_user_edited(self, *_args) -> None:
        if self._tracking_edits:
            self._user_edited = True
            self._update_save_draft_enabled()

    def _on_form_field_changed(self, *_args) -> None:
        self._mark_user_edited()
        self._update_field_hints()
        self._update_send_enabled()

    @staticmethod
    def _required_address_hint(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return _TO_PLACEHOLDER
        try:
            if not parse_address_list(text):
                return _TO_PLACEHOLDER
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _optional_address_hint(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            parse_address_list(text)
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _subject_hint_text(text: str) -> str | None:
        if not text.strip():
            return "Subject is required"
        return None

    def _apply_entry_hint(
        self,
        entry: Gtk.Entry,
        message: str | None,
        *,
        default_placeholder: str,
    ) -> None:
        if message is None:
            entry.set_placeholder_text(default_placeholder)
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
            entry.set_tooltip_text(None)
            return
        if not entry.get_text().strip():
            entry.set_placeholder_text(message)
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
            entry.set_tooltip_text(None)
            return
        entry.set_placeholder_text(default_placeholder)
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY,
            "dialog-warning-symbolic",
        )
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, message)
        entry.set_tooltip_text(message)

    def _update_field_hints(self) -> None:
        self._apply_entry_hint(
            self._to_entry,
            self._required_address_hint(self._to_entry.get_text()),
            default_placeholder=_TO_PLACEHOLDER,
        )
        if self._cc_row.get_visible():
            self._apply_entry_hint(
                self._cc_entry,
                self._optional_address_hint(self._cc_entry.get_text()),
                default_placeholder=_CC_PLACEHOLDER,
            )
        else:
            self._apply_entry_hint(
                self._cc_entry, None, default_placeholder=_CC_PLACEHOLDER
            )
        if self._bcc_row.get_visible():
            self._apply_entry_hint(
                self._bcc_entry,
                self._optional_address_hint(self._bcc_entry.get_text()),
                default_placeholder=_BCC_PLACEHOLDER,
            )
        else:
            self._apply_entry_hint(
                self._bcc_entry, None, default_placeholder=_BCC_PLACEHOLDER
            )
        self._apply_entry_hint(
            self._subject_entry,
            self._subject_hint_text(self._subject_entry.get_text()),
            default_placeholder=_SUBJECT_PLACEHOLDER,
        )

    def _validate_send_fields(self) -> bool:
        if not self._selected_account().can_send:
            return False
        if self._required_address_hint(self._to_entry.get_text()) is not None:
            return False
        if self._optional_address_hint(self._cc_entry.get_text()) is not None:
            return False
        if self._optional_address_hint(self._bcc_entry.get_text()) is not None:
            return False
        return self._subject_hint_text(self._subject_entry.get_text()) is None

    def _update_send_enabled(self) -> None:
        if self._saving_draft:
            return
        self._send_btn.set_sensitive(self._validate_send_fields())

    def _update_save_draft_enabled(self) -> None:
        if self._saving_draft:
            return
        self._save_draft_btn.set_sensitive(self._user_edited)

    def _prefill_fields(self) -> None:
        self._tracking_edits = False
        try:
            self._prefill_fields_impl()
        finally:
            self._tracking_edits = True
        self._update_field_hints()
        self._update_send_enabled()
        self._update_save_draft_enabled()

    def _prefill_fields_impl(self) -> None:
        account = self._selected_account()
        signature = get_account_signature(account.uid)
        if self._mode in ("reply", "reply-all") and self._reply_to is not None:
            try:
                if self._mode == "reply-all":
                    to_addrs, cc_addrs = build_reply_all_recipients(
                        self._reply_to,
                        own_addresses=self._own_addresses(),
                    )
                    self._to_entry.set_text(format_address_list(to_addrs))
                    if cc_addrs:
                        self._show_cc_field(format_address_list(cc_addrs))
                else:
                    self._to_entry.set_text(
                        format_address_list(
                            extract_reply_target_addresses(self._reply_to)
                        )
                    )
            except ValueError as exc:
                self._show_error(str(exc))
            self._subject_entry.set_text(
                build_reply_subject(self._reply_to.get("subject") or "")
            )
            quoted = quote_plain_reply(
                self._reply_to,
                body_text_for_quoting(self._reply_to),
            )
            self._quoted_html_source = body_html_for_quoting(self._reply_to)
            self._quoted_plain_expected = quoted
            body = compose_body_with_signature(
                mode=self._mode,
                quoted_body=quoted,
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)
            self._place_body_cursor_at_start()
        elif self._mode == "forward" and self._reply_to is not None:
            self._subject_entry.set_text(
                build_forward_subject(self._reply_to.get("subject") or "")
            )
            quoted = quote_plain_forward(
                self._reply_to,
                body_text_for_quoting(self._reply_to),
            )
            self._quoted_html_source = body_html_for_quoting(self._reply_to)
            self._quoted_plain_expected = quoted
            body = compose_body_with_signature(
                mode=self._mode,
                quoted_body=quoted,
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)
            self._place_body_cursor_at_start()
        elif self._mode == "draft" and self._draft_message is not None:
            msg = self._draft_message
            if msg.get("to"):
                self._to_entry.set_text(str(msg["to"]))
            if msg.get("cc"):
                self._show_cc_field(str(msg["cc"]))
            if msg.get("bcc"):
                self._bcc_entry.set_text(str(msg["bcc"]))
                self._bcc_row.set_visible(True)
                self._bcc_entry.set_can_focus(True)
                self._bcc_toggle_btn.set_label("Hide Bcc")
            self._subject_entry.set_text(str(msg.get("subject") or ""))
            plain_body = str(msg.get("body_plain") or "")
            self._body_view.get_buffer().set_text(plain_body)
            self._place_body_cursor_at_start()
            self._draft_body_html = (msg.get("body_html") or "").strip() or None
            self._draft_body_plain_snapshot = plain_body
            self._quoted_html_source = None
            self._quoted_plain_expected = ""
        elif self._mode == "send-again" and self._draft_message is not None:
            msg = self._draft_message
            if msg.get("to"):
                self._to_entry.set_text(str(msg["to"]))
            if msg.get("cc"):
                self._show_cc_field(str(msg["cc"]))
            if msg.get("bcc"):
                self._bcc_entry.set_text(str(msg["bcc"]))
                self._bcc_row.set_visible(True)
                self._bcc_entry.set_can_focus(True)
                self._bcc_toggle_btn.set_label("Hide Bcc")
            self._subject_entry.set_text(str(msg.get("subject") or ""))
            plain_body = str(msg.get("body_plain") or "")
            self._body_view.get_buffer().set_text(plain_body)
            self._place_body_cursor_at_start()
            self._draft_body_html = (msg.get("body_html") or "").strip() or None
            self._draft_body_plain_snapshot = plain_body
            self._quoted_html_source = None
            self._quoted_plain_expected = ""
        elif self._mode == "outbox" and self._outbox_queue_id is not None:
            queued = load_queued_outbound_message(self._outbox_queue_id)
            if queued.to:
                self._to_entry.set_text(format_address_list(queued.to))
            if queued.cc:
                self._show_cc_field(format_address_list(queued.cc))
            if queued.bcc:
                self._bcc_entry.set_text(format_address_list(queued.bcc))
                self._bcc_row.set_visible(True)
                self._bcc_entry.set_can_focus(True)
                self._bcc_toggle_btn.set_label("Hide Bcc")
            self._subject_entry.set_text(queued.subject)
            self._body_view.get_buffer().set_text(queued.body)
            self._place_body_cursor_at_start()
            self._attachments = load_queued_attachments(
                self._outbox_queue_id, queued
            )
            self._rebuild_attachment_rows()
        else:
            mailto = self._mailto
            if mailto is not None:
                if mailto.to:
                    self._to_entry.set_text(format_address_list(list(mailto.to)))
                if mailto.cc:
                    self._show_cc_field(format_address_list(list(mailto.cc)))
                if mailto.bcc:
                    self._bcc_entry.set_text(format_address_list(list(mailto.bcc)))
                    self._bcc_row.set_visible(True)
                    self._bcc_entry.set_can_focus(True)
                    self._bcc_toggle_btn.set_label("Hide Bcc")
                if mailto.subject:
                    self._subject_entry.set_text(mailto.subject)
            body = compose_body_with_signature(
                mode="new",
                quoted_body="",
                signature=signature,
            )
            if mailto is not None and mailto.body:
                # Signature block (if any) is "\n\n…"; keep mailto body above it.
                body = mailto.body + body
            self._set_body_plain_text(body)
            self._tracked_signature = normalize_signature_text(signature) or None
            self._place_signature_mark()
            self._place_body_cursor_at_start()

    def _place_body_cursor_at_start(self) -> None:
        buffer = self._body_view.get_buffer()
        buffer.place_cursor(buffer.get_start_iter())

    def _on_subject_focus_leave(self, *_args) -> None:
        if self._mode == "new":
            self._focus_body_at_start_on_enter = True

    def _on_body_focus_in(self, *_args) -> None:
        if not self._focus_body_at_start_on_enter:
            return
        self._focus_body_at_start_on_enter = False
        if self._mode == "new":
            self._place_body_cursor_at_start()

    def _show_cc_field(self, text: str) -> None:
        self._cc_entry.set_text(text)
        self._cc_row.set_visible(True)
        self._cc_entry.set_can_focus(True)
        self._cc_toggle_btn.set_label("Hide Cc")

    def _on_toggle_cc(self, button: Gtk.Button) -> None:
        reveal = not self._cc_row.get_visible()
        self._cc_row.set_visible(reveal)
        self._cc_entry.set_can_focus(reveal)
        button.set_label("Hide Cc" if reveal else "Cc")
        self._update_field_hints()
        if reveal:
            self._cc_entry.grab_focus()

    def _on_toggle_bcc(self, button: Gtk.Button) -> None:
        reveal = not self._bcc_row.get_visible()
        self._bcc_row.set_visible(reveal)
        self._bcc_entry.set_can_focus(reveal)
        button.set_label("Hide Bcc" if reveal else "Bcc")
        self._update_field_hints()
        if reveal:
            self._bcc_entry.grab_focus()

    def is_dirty(self) -> bool:
        return self._user_edited

    def force_close(self) -> None:
        """Close immediately, e.g. when the application is quitting."""
        self._cancel_draft_save()
        self._force_close = True
        self.destroy()

    def _dismiss(self) -> None:
        """Close the compose window without re-entering close-request."""
        self._cancel_draft_save()
        self.destroy()

    def _cancel_draft_save(self) -> None:
        cancellable = self._draft_save_cancellable
        if cancellable is not None and not cancellable.is_cancelled():
            cancellable.cancel()
        self._clear_draft_save_timeout()
        self._draft_save_generation += 1
        self._saving_draft = False
        self._close_when_saved = False

    def _clear_draft_save_timeout(self) -> None:
        timeout_id = self._draft_save_timeout_id
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
            self._draft_save_timeout_id = None

    def _set_compose_actions_sensitive(self, sensitive: bool) -> None:
        if not sensitive:
            self._save_draft_btn.set_sensitive(False)
            self._send_btn.set_sensitive(False)
            self._attach_files_btn.set_sensitive(False)
            self._attachments_box.set_sensitive(False)
            return
        self._attach_files_btn.set_sensitive(True)
        self._attachments_box.set_sensitive(True)
        self._update_send_enabled()
        self._update_save_draft_enabled()

    def _on_close_request(self, *_args) -> bool:
        """Window manager / header close: block while busy or when prompting."""
        if self._force_close:
            return False
        if self._saving_draft:
            self._prompt_close_while_saving()
            return True
        if not self._user_edited:
            return False
        self._prompt_save_before_close()
        return True

    def _prompt_close_while_saving(self) -> None:
        if self._close_while_saving_dialog is not None:
            return
        dialog = Adw.AlertDialog(
            heading="Saving Draft…",
            body="Draft save is still in progress.",
            close_response="wait",
        )
        dialog.add_response("wait", "Keep Waiting")
        dialog.add_response("close", "Close Anyway")
        dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("wait")
        self._close_while_saving_dialog = dialog
        dialog.connect("response", self._on_close_while_saving_response)
        dialog.present(self)

    def _on_close_while_saving_response(
        self, dialog: Adw.AlertDialog, response: str
    ) -> None:
        self._close_while_saving_dialog = None
        if response == "close":
            self._dismiss()

    def _prompt_save_before_close(self) -> None:
        if self._unsaved_dialog is not None:
            return
        dialog = Adw.AlertDialog(
            heading="Save draft?",
            body="Save your changes to Drafts before closing?",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save Draft")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("save")
        self._unsaved_dialog = dialog
        dialog.connect("response", self._on_unsaved_close_response)
        dialog.present(self)

    def _on_unsaved_close_response(self, dialog: Adw.AlertDialog, response: str) -> None:
        self._unsaved_dialog = None
        if response == "discard":
            self._dismiss()
        elif response == "save":
            self._begin_save_draft(close_when_done=True)

    def _on_save_draft_clicked(self, *_args) -> None:
        self._begin_save_draft(close_when_done=False)

    def _resolve_outbound_body_html(self, body_plain: str) -> str | None:
        if self._mode in ("reply", "reply-all", "forward"):
            return build_outbound_html_for_compose(
                body_plain=body_plain,
                mode=self._mode,
                reply_to=self._reply_to,
                quoted_html_source=self._quoted_html_source,
                quoted_plain_expected=self._quoted_plain_expected,
            )
        if self._mode in ("draft", "send-again"):
            if (
                body_plain == self._draft_body_plain_snapshot
                and self._draft_body_html
                and not is_plain_wrapper_html(self._draft_body_html, body_plain)
            ):
                return self._draft_body_html
            return None
        return None

    def _collect_draft_fields(
        self,
        *,
        for_draft: bool = True,
    ) -> tuple[
        list[str],
        list[str],
        list[str],
        str,
        str,
        str | None,
        str | None,
        str | None,
    ]:
        parse_addresses = (
            parse_draft_address_list if for_draft else parse_address_list
        )
        to_addrs = parse_addresses(self._to_entry.get_text())
        cc_addrs = parse_addresses(self._cc_entry.get_text())
        bcc_addrs = parse_addresses(self._bcc_entry.get_text())
        subject = self._subject_entry.get_text().strip()
        buffer = self._body_view.get_buffer()
        start, end = buffer.get_bounds()
        body = buffer.get_text(start, end, False)
        body_html = self._resolve_outbound_body_html(body)

        in_reply_to = None
        references = None
        if self._mode in ("reply", "reply-all") and self._reply_to is not None:
            in_reply_to = normalize_in_reply_to(self._reply_to.get("message_id"))
            references = build_reply_references(
                in_reply_to,
                self._reply_to.get("references"),
            )
        elif self._mode == "draft" and self._draft_message is not None:
            in_reply_to = normalize_in_reply_to(self._draft_message.get("message_id"))
            references = normalize_references_header(
                self._draft_message.get("references"),
            )

        return (
            to_addrs,
            cc_addrs,
            bcc_addrs,
            subject,
            body,
            body_html,
            in_reply_to,
            references,
        )

    def _begin_save_draft(self, *, close_when_done: bool) -> None:
        if self._saving_draft:
            return

        try:
            (
                to_addrs,
                cc_addrs,
                bcc_addrs,
                subject,
                body,
                body_html,
                in_reply_to,
                references,
            ) = self._collect_draft_fields()
        except ValueError as exc:
            self._show_error(str(exc))
            self._close_when_saved = False
            return

        account = self._selected_account()

        self._close_when_saved = close_when_done
        self._draft_save_generation += 1
        generation = self._draft_save_generation
        cancellable = Gio.Cancellable()
        self._draft_save_cancellable = cancellable
        self._saving_draft = True
        self._set_compose_actions_sensitive(False)
        self._set_status("Saving draft…")
        self._pending_draft_body = body
        self._pending_draft_body_html = body_html
        self._clear_draft_save_timeout()
        self._draft_save_timeout_id = GLib.timeout_add_seconds(
            30, self._on_draft_save_watchdog, generation
        )

        account_uid = account.uid
        existing_uid = self._draft_message_uid if self._mode == "draft" else None
        drafts_folder_name = self._draft_folder_name if self._mode == "draft" else None
        attachments = list(self._attachments)

        if self._on_draft_save_started is not None:
            self._on_draft_save_started(account_uid, drafts_folder_name)

        get_mail_io_thread().submit(
            self._run_save_draft_on_mail_thread,
            generation=generation,
            cancellable=cancellable,
            account_uid=account_uid,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            existing_uid=existing_uid,
            drafts_folder_name=drafts_folder_name,
            attachments=attachments,
        )

    def _on_draft_save_watchdog(self, generation: int) -> bool:
        self._draft_save_timeout_id = None
        if generation != self._draft_save_generation or not self._saving_draft:
            return False
        cancellable = self._draft_save_cancellable
        if cancellable is not None and not cancellable.is_cancelled():
            cancellable.cancel()
        self._set_status("Draft save timed out — will retry from local queue if saved")
        show_toast(self, "Draft save timed out")
        return False

    def _run_save_draft_on_mail_thread(
        self,
        *,
        generation: int,
        cancellable: Gio.Cancellable,
        account_uid: str,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str,
        body: str,
        body_html: str | None,
        in_reply_to: str | None,
        references: str | None,
        existing_uid: str | None,
        drafts_folder_name: str | None,
        attachments: list[ComposeAttachment],
    ) -> None:
        error: Exception | None = None
        result: tuple[str, str] | None = None
        try:
            result = self._mail.save_draft(
                account_uid,
                to=to_addrs or None,
                cc=cc_addrs or None,
                bcc=bcc_addrs or None,
                subject=subject,
                body=body,
                body_html=body_html,
                in_reply_to=in_reply_to,
                references=references,
                existing_uid=existing_uid,
                drafts_folder_name=drafts_folder_name,
                attachments=attachments or None,
                cancellable=cancellable,
            )
        except Exception as exc:
            log.warning("Save draft failed: %s", exc)
            error = exc
        GLib.idle_add(
            self._on_save_draft_finished, generation, error, result
        )

    def _on_save_draft_finished(
        self,
        generation: int,
        error: Exception | None,
        result: tuple[str, str] | None,
    ) -> bool:
        if generation != self._draft_save_generation:
            return False
        self._clear_draft_save_timeout()
        self._draft_save_cancellable = None
        self._saving_draft = False
        self._set_compose_actions_sensitive(True)
        close_when_done = self._close_when_saved
        self._close_when_saved = False
        if error is not None:
            self._set_status("Could not save draft")
            self._show_error(str(error))
            return False

        assert result is not None
        previous_uid = self._draft_message_uid
        self._draft_folder_name, self._draft_message_uid = result
        self._user_edited = False
        self._draft_body_plain_snapshot = self._pending_draft_body
        self._draft_body_html = self._pending_draft_body_html
        self._update_save_draft_enabled()
        if is_queued_draft_id(self._draft_message_uid):
            queued_status = "Draft saved — will sync to Drafts when online"
            self._set_status(queued_status)
            show_toast(self, queued_status)
        else:
            self._set_status("Draft saved")
            show_toast(self, "Draft saved")
        if self._on_draft_saved is not None:
            account = self._selected_account()
            self._on_draft_saved(
                SavedDraftNotification(
                    account_uid=account.uid,
                    folder_name=self._draft_folder_name,
                    uid=self._draft_message_uid,
                    previous_uid=previous_uid,
                    subject=self._subject_entry.get_text().strip() or "(no subject)",
                    to=self._to_entry.get_text().strip(),
                    from_label=account.from_label,
                    has_attachments=bool(self._attachments),
                    sort_date=time.time(),
                )
            )
        if close_when_done:
            self._dismiss()
        return False

    def _on_send_clicked(self, *_args) -> None:
        try:
            (
                to_addrs,
                cc_addrs,
                bcc_addrs,
                subject,
                body,
                body_html,
                in_reply_to,
                references,
            ) = self._collect_draft_fields(for_draft=False)
        except ValueError as exc:
            self._show_error(str(exc))
            return

        if not to_addrs:
            self._show_error(_TO_PLACEHOLDER)
            return

        if not subject:
            self._show_error("Subject is required")
            return

        account = self._selected_account()
        if not account.can_send:
            self._show_error("This account has no mail transport configured")
            return

        try:
            validate_compose_mime_fields(
                from_name=account.from_name,
                subject=subject,
                to=to_addrs,
                cc=cc_addrs,
                bcc=bcc_addrs,
                in_reply_to=in_reply_to,
                references=references,
                attachments=list(self._attachments) or None,
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return

        if not self._attachments and body_mentions_attachment(body, mode=self._mode):
            self._prompt_send_without_attachments(
                account=account,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                bcc_addrs=bcc_addrs,
                subject=subject,
                body=body,
                body_html=body_html,
                in_reply_to=in_reply_to,
                references=references,
            )
            return

        self._proceed_with_send(
            account=account,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
        )

    def _prompt_send_without_attachments(
        self,
        *,
        account: MailAccount,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str,
        body: str,
        body_html: str | None,
        in_reply_to: str | None,
        references: str | None,
    ) -> None:
        if self._missing_attachment_dialog is not None:
            return
        dialog = Adw.AlertDialog(
            heading="Send Without Attachments?",
            body="Your message mentions an attachment, but no files are attached.",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("send", "Send Anyway")
        dialog.set_default_response("cancel")
        self._missing_attachment_dialog = dialog
        pending = (
            account,
            to_addrs,
            cc_addrs,
            bcc_addrs,
            subject,
            body,
            body_html,
            in_reply_to,
            references,
        )
        dialog.connect(
            "response",
            self._on_send_without_attachments_response,
            pending,
        )
        dialog.present(self)

    def _on_send_without_attachments_response(
        self,
        dialog: Adw.AlertDialog,
        response: str,
        pending: tuple[
            MailAccount,
            list[str],
            list[str],
            list[str],
            str,
            str,
            str | None,
            str | None,
            str | None,
        ],
    ) -> None:
        self._missing_attachment_dialog = None
        if response != "send":
            return
        (
            account,
            to_addrs,
            cc_addrs,
            bcc_addrs,
            subject,
            body,
            body_html,
            in_reply_to,
            references,
        ) = pending
        self._proceed_with_send(
            account=account,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
        )

    def _proceed_with_send(
        self,
        *,
        account: MailAccount,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        subject: str,
        body: str,
        body_html: str | None,
        in_reply_to: str | None,
        references: str | None,
    ) -> None:
        parent = self._parent_window
        request = OutboundSendRequest(
            account_uid=account.uid,
            to=to_addrs,
            cc=cc_addrs or None,
            bcc=bcc_addrs or None,
            subject=subject,
            body=body,
            body_html=body_html,
            in_reply_to=in_reply_to,
            references=references,
            attachments=list(self._attachments) or None,
            draft_folder=self._draft_folder_name if self._mode == "draft" else None,
            draft_uid=self._draft_message_uid if self._mode == "draft" else None,
            queue_id=self._outbox_queue_id,
        )

        self._set_status("Sending message…")
        self._dismiss()
        run_outbound_send(
            mail=self._mail,
            parent=parent,
            set_status=self._set_status,
            on_outbox_changed=self._on_outbox_changed,
            on_draft_saved=self._on_draft_saved,
            on_delayed_send=self._on_delayed_send,
            request=request,
        )

    def _show_error(self, message: str) -> None:
        show_error_toast(self, message)
