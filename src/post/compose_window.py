# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compose window for new messages and replies."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any, Literal

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gio, GLib, Gtk

from post.icon_utils import apply_window_icon
from post.mail import MailService
from post.mail.compose import (
    ComposeAttachment,
    body_is_unedited_signature_template,
    build_forward_subject,
    build_reply_all_recipients,
    build_reply_references,
    build_reply_subject,
    compose_body_with_signature,
    extract_reply_address,
    format_address_list,
    guess_attachment_mime_type,
    normalize_email,
    parse_address_list,
    quote_plain_forward,
    quote_plain_reply,
)
from post.mail.helpers import format_attachment_size
from post.mail.correspondents import (
    Correspondent,
    apply_address_completion,
    current_address_token,
    match_correspondents,
)
from post.mail.eds import MailAccount
from post.mail.send_errors import SendQueued, user_send_error_message
from post.preferences import get_account_signature, get_account_signatures
from post.toast import show_error_toast

log = logging.getLogger(__name__)

ComposeMode = Literal["new", "reply", "reply-all", "forward", "draft"]
SetStatus = Callable[[str], None]
OnDraftSaved = Callable[[], None]

_LABEL_WIDTH = 72
_TO_PLACEHOLDER = "Add a recipient in the To field."
_CC_PLACEHOLDER = "Optional"
_BCC_PLACEHOLDER = "Optional"
_SUBJECT_PLACEHOLDER = "Subject is required"


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
        mode: ComposeMode = "new",
        reply_to: dict[str, Any] | None = None,
        draft_folder_name: str | None = None,
        draft_message_uid: str | None = None,
        draft_message: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(transient_for=parent, modal=False)
        apply_window_icon(self)
        self._mail = mail
        self._set_status = set_status
        self._on_outbox_changed = on_outbox_changed
        self._on_draft_saved = on_draft_saved
        self._mode = mode
        self._reply_to = reply_to
        self._draft_folder_name = draft_folder_name
        self._draft_message_uid = draft_message_uid
        self._draft_message = draft_message
        self._sending = False
        self._saving_draft = False
        self._attachments: list[ComposeAttachment] = []
        self._close_when_saved = False
        self._unsaved_dialog: Adw.AlertDialog | None = None
        self._user_edited = False
        self._tracking_edits = False
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
        else:
            title = "New Message"
        self.set_title(title)
        self.set_default_size(720, 560)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Gtk.Label(label=title))

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)
        self._cancel_btn = cancel_btn

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.connect("clicked", self._on_send_clicked)
        header.pack_end(self._send_btn)

        self._save_draft_btn = Gtk.Button(label="Save Draft")
        self._save_draft_btn.connect("clicked", self._on_save_draft_clicked)
        header.pack_end(self._save_draft_btn)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form.set_margin_start(18)
        form.set_margin_end(18)
        form.set_margin_top(12)
        form.set_margin_bottom(12)
        scrolled.set_child(form)

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
        form.append(self._labeled_row("From", self._from_dropdown))

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
        form.append(self._to_row)

        self._cc_entry = Gtk.Entry()
        self._cc_entry.set_placeholder_text(_CC_PLACEHOLDER)
        self._cc_entry.set_hexpand(True)
        self._cc_entry.set_can_focus(False)
        self._cc_entry.connect("changed", self._on_form_field_changed)
        self._cc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._cc_row.append(self._field_label("Cc"))
        self._cc_row.append(self._cc_entry)
        self._cc_row.set_visible(False)
        form.append(self._cc_row)
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
        form.append(self._bcc_row)
        self._setup_address_completion(self._bcc_entry)

        self._subject_entry = Gtk.Entry()
        self._subject_entry.set_placeholder_text(_SUBJECT_PLACEHOLDER)
        self._subject_entry.connect("changed", self._on_form_field_changed)
        form.append(self._labeled_row("Subject", self._subject_entry))

        self._focus_body_at_start_on_enter = False
        subject_focus = Gtk.EventControllerFocus()
        subject_focus.connect("leave", self._on_subject_focus_leave)
        self._subject_entry.add_controller(subject_focus)

        body_frame = Gtk.Frame()
        body_frame.add_css_class("view")
        body_scroll = Gtk.ScrolledWindow()
        body_scroll.set_min_content_height(240)
        body_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._body_view = Gtk.TextView()
        self._body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._body_view.set_left_margin(8)
        self._body_view.set_right_margin(8)
        self._body_view.set_top_margin(8)
        self._body_view.set_bottom_margin(8)
        self._body_view.get_buffer().connect("changed", self._mark_user_edited)
        body_scroll.set_child(self._body_view)
        body_frame.set_child(body_scroll)
        body_frame.set_vexpand(True)
        form.append(body_frame)

        attachments_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._attach_files_btn = Gtk.Button(label="Attach Files…")
        self._attach_files_btn.set_halign(Gtk.Align.START)
        self._attach_files_btn.connect("clicked", self._on_attach_clicked)
        attachments_section.append(self._attach_files_btn)
        self._attachments_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4
        )
        self._attachments_box.set_visible(False)
        attachments_section.append(self._attachments_box)
        form.append(attachments_section)

        body_focus = Gtk.EventControllerFocus()
        body_focus.connect("enter", self._on_body_focus_in)
        self._body_view.add_controller(body_focus)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(scrolled)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar_view)
        self.set_content(self._toast_overlay)

        self._prefill_fields()
        self._tracking_edits = True
        self._load_correspondents()
        self._update_field_hints()
        self._update_send_enabled()
        self.connect("close-request", self._on_close_request)
        GLib.idle_add(self._set_initial_focus)
        if self._mode == "draft":
            GLib.idle_add(self._begin_load_draft_attachments)

    def _on_attach_clicked(self, *_args) -> None:
        if self._sending or self._saving_draft:
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
            row.add_css_class("linked")
            icon = Gtk.Image.new_from_icon_name("mail-attachment-symbolic")
            icon.add_css_class("dim-label")
            row.append(icon)
            label = Gtk.Label(
                label=f"{attachment.filename} ({format_attachment_size(len(attachment.data))})"
            )
            label.set_xalign(0)
            label.set_hexpand(True)
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
            row.append(label)
            remove_btn = Gtk.Button(icon_name="window-close-symbolic")
            remove_btn.set_tooltip_text("Remove Attachment")
            remove_btn.add_css_class("flat")
            remove_btn.connect("clicked", self._on_remove_attachment, index)
            row.append(remove_btn)
            self._attachments_box.append(row)

    def _on_remove_attachment(self, _button: Gtk.Button, index: int) -> None:
        if self._sending or self._saving_draft:
            return
        if index < 0 or index >= len(self._attachments):
            return
        del self._attachments[index]
        self._mark_user_edited()
        self._refresh_attachments_ui()

    def _begin_load_draft_attachments(self) -> bool:
        if self._mode != "draft" or self._draft_message is None:
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

        threading.Thread(target=worker, daemon=True).start()
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
        if self._mode == "draft":
            self._body_view.grab_focus()
            buffer = self._body_view.get_buffer()
            buffer.place_cursor(buffer.get_start_iter())
            return False
        if self._mode in ("reply", "reply-all"):
            self._body_view.grab_focus()
            buffer = self._body_view.get_buffer()
            buffer.place_cursor(buffer.get_start_iter())
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
        self._mark_user_edited()
        self._load_correspondents()
        if self._mode == "new":
            self._refresh_signature_for_account(self._selected_account())
        self._update_send_enabled()

    def _known_signatures(self) -> list[str]:
        return list(get_account_signatures().values())

    def _refresh_signature_for_account(self, account: MailAccount) -> None:
        buffer = self._body_view.get_buffer()
        start, end = buffer.get_bounds()
        current = buffer.get_text(start, end, False)
        if not body_is_unedited_signature_template(current, self._known_signatures()):
            return
        body = compose_body_with_signature(
            mode="new",
            quoted_body="",
            signature=get_account_signature(account.uid),
        )
        self._tracking_edits = False
        buffer.set_text(body)
        self._tracking_edits = True

    def _load_correspondents(self) -> None:
        account = self._selected_account()
        generation = self._correspondents_generation + 1
        self._correspondents_generation = generation
        self._correspondents = []
        self._set_completion_model(Gtk.ListStore(str))
        self._refresh_address_completions()
        account_uid = account.uid

        def worker() -> None:
            try:
                correspondents = self._mail.get_correspondents(account_uid)
            except Exception as exc:
                log.debug(
                    "Could not load address suggestions for account %s: %s",
                    account_uid,
                    exc,
                )
                correspondents = []
            GLib.idle_add(
                self._on_correspondents_loaded,
                generation,
                correspondents,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_correspondents_loaded(
        self,
        generation: int,
        correspondents: list[Correspondent],
    ) -> bool:
        if generation != self._correspondents_generation:
            return False
        self._correspondents = correspondents
        model = Gtk.ListStore(str)
        for correspondent in correspondents:
            model.append([correspondent.display])
        self._set_completion_model(model)
        self._refresh_address_completions()
        return False

    @classmethod
    def _field_label(cls, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=1)
        label.set_size_request(_LABEL_WIDTH, -1)
        label.set_halign(Gtk.Align.END)
        label.set_valign(Gtk.Align.CENTER)
        if text:
            label.add_css_class("heading")
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

    def _on_form_field_changed(self, *_args) -> None:
        self._mark_user_edited()
        self._update_field_hints()
        self._update_send_enabled()

    @staticmethod
    def _required_address_hint(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return "Add a recipient in the To field."
        try:
            if not parse_address_list(text):
                return "Add a recipient in the To field."
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
        if self._sending or self._saving_draft:
            return
        self._send_btn.set_sensitive(self._validate_send_fields())

    def _prefill_fields(self) -> None:
        self._tracking_edits = False
        try:
            self._prefill_fields_impl()
        finally:
            self._tracking_edits = True
        self._update_field_hints()
        self._update_send_enabled()

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
                        self._cc_entry.set_text(format_address_list(cc_addrs))
                        self._cc_row.set_visible(True)
                        self._cc_entry.set_can_focus(True)
                        self._cc_toggle_btn.set_label("Hide Cc")
                else:
                    self._to_entry.set_text(
                        extract_reply_address(self._reply_to.get("from", ""))
                    )
            except ValueError as exc:
                self._show_error(str(exc))
            self._subject_entry.set_text(
                build_reply_subject(self._reply_to.get("subject") or "")
            )
            quoted = quote_plain_reply(
                self._reply_to,
                self._reply_to.get("body_plain"),
            )
            body = compose_body_with_signature(
                mode=self._mode,
                quoted_body=quoted,
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)
        elif self._mode == "forward" and self._reply_to is not None:
            self._subject_entry.set_text(
                build_forward_subject(self._reply_to.get("subject") or "")
            )
            quoted = quote_plain_forward(
                self._reply_to,
                self._reply_to.get("body_plain"),
            )
            body = compose_body_with_signature(
                mode=self._mode,
                quoted_body=quoted,
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)
        elif self._mode == "draft" and self._draft_message is not None:
            msg = self._draft_message
            if msg.get("to"):
                self._to_entry.set_text(str(msg["to"]))
            if msg.get("cc"):
                self._cc_entry.set_text(str(msg["cc"]))
                self._cc_row.set_visible(True)
                self._cc_entry.set_can_focus(True)
                self._cc_toggle_btn.set_label("Hide Cc")
            if msg.get("bcc"):
                self._bcc_entry.set_text(str(msg["bcc"]))
                self._bcc_row.set_visible(True)
                self._bcc_entry.set_can_focus(True)
                self._bcc_toggle_btn.set_label("Hide Bcc")
            self._subject_entry.set_text(str(msg.get("subject") or ""))
            self._body_view.get_buffer().set_text(str(msg.get("body_plain") or ""))
        else:
            body = compose_body_with_signature(
                mode="new",
                quoted_body="",
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)
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

    def _dismiss(self) -> None:
        """Close the compose window without re-entering close-request."""
        self.destroy()

    def _request_dismiss(self) -> None:
        """Cancel button and explicit dismiss: prompt only when the user edited."""
        if self._sending or self._saving_draft:
            return
        if not self._user_edited:
            self._dismiss()
            return
        self._prompt_save_before_close()

    def _set_compose_actions_sensitive(self, sensitive: bool) -> None:
        if not sensitive:
            self._cancel_btn.set_sensitive(False)
            self._save_draft_btn.set_sensitive(False)
            self._send_btn.set_sensitive(False)
            self._attach_files_btn.set_sensitive(False)
            self._attachments_box.set_sensitive(False)
            return
        self._cancel_btn.set_sensitive(True)
        self._save_draft_btn.set_sensitive(True)
        self._attach_files_btn.set_sensitive(True)
        self._attachments_box.set_sensitive(True)
        self._update_send_enabled()

    def _on_close_request(self, *_args) -> bool:
        """Window manager / header close: block while busy or when prompting."""
        if self._sending or self._saving_draft:
            return True
        if not self._user_edited:
            return False
        self._prompt_save_before_close()
        return True

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

    def _on_cancel_clicked(self, *_args) -> None:
        self._request_dismiss()

    def _on_save_draft_clicked(self, *_args) -> None:
        self._begin_save_draft(close_when_done=False)

    def _collect_draft_fields(
        self,
    ) -> tuple[list[str], list[str], list[str], str, str, str | None, str | None]:
        to_addrs = parse_address_list(self._to_entry.get_text())
        cc_addrs = parse_address_list(self._cc_entry.get_text())
        bcc_addrs = parse_address_list(self._bcc_entry.get_text())
        subject = self._subject_entry.get_text().strip()
        buffer = self._body_view.get_buffer()
        start, end = buffer.get_bounds()
        body = buffer.get_text(start, end, False)

        in_reply_to = None
        references = None
        if self._mode in ("reply", "reply-all") and self._reply_to is not None:
            in_reply_to = self._reply_to.get("message_id")
            references = build_reply_references(
                in_reply_to,
                self._reply_to.get("references"),
            )
        elif self._mode == "draft" and self._draft_message is not None:
            in_reply_to = self._draft_message.get("message_id")
            references = self._draft_message.get("references")

        return (
            to_addrs,
            cc_addrs,
            bcc_addrs,
            subject,
            body,
            in_reply_to,
            references,
        )

    def _begin_save_draft(self, *, close_when_done: bool) -> None:
        if self._saving_draft or self._sending:
            return

        try:
            (
                to_addrs,
                cc_addrs,
                bcc_addrs,
                subject,
                body,
                in_reply_to,
                references,
            ) = self._collect_draft_fields()
        except ValueError as exc:
            self._show_error(str(exc))
            self._close_when_saved = False
            return

        account = self._selected_account()
        if not account.can_send:
            self._show_error("This account has no mail transport configured")
            self._close_when_saved = False
            return

        self._close_when_saved = close_when_done
        self._saving_draft = True
        self._set_compose_actions_sensitive(False)
        self._set_status("Saving draft…")

        account_uid = account.uid
        existing_uid = self._draft_message_uid
        drafts_folder_name = self._draft_folder_name
        attachments = list(self._attachments)

        def worker() -> None:
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
                    in_reply_to=in_reply_to,
                    references=references,
                    existing_uid=existing_uid,
                    drafts_folder_name=drafts_folder_name,
                    attachments=attachments or None,
                )
            except Exception as exc:
                log.warning("Save draft failed: %s", exc)
                error = exc
            GLib.idle_add(self._on_save_draft_finished, error, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_save_draft_finished(
        self,
        error: Exception | None,
        result: tuple[str, str] | None,
    ) -> bool:
        self._saving_draft = False
        self._set_compose_actions_sensitive(True)
        close_when_done = self._close_when_saved
        self._close_when_saved = False
        if error is not None:
            self._show_error(str(error))
            return False

        assert result is not None
        self._draft_folder_name, self._draft_message_uid = result
        self._user_edited = False
        self._set_status("Draft saved")
        if self._on_draft_saved is not None:
            self._on_draft_saved()
        if close_when_done:
            self._dismiss()
        return False

    def _on_send_clicked(self, *_args) -> None:
        if self._sending:
            return

        try:
            to_addrs = parse_address_list(self._to_entry.get_text())
            cc_addrs = parse_address_list(self._cc_entry.get_text())
            bcc_addrs = parse_address_list(self._bcc_entry.get_text())
        except ValueError as exc:
            self._show_error(str(exc))
            return

        if not to_addrs:
            self._show_error("Add a recipient in the To field.")
            return

        subject = self._subject_entry.get_text().strip()
        if not subject:
            self._show_error("Subject is required")
            return

        buffer = self._body_view.get_buffer()
        start, end = buffer.get_bounds()
        body = buffer.get_text(start, end, False)

        in_reply_to = None
        references = None
        if self._mode in ("reply", "reply-all") and self._reply_to is not None:
            in_reply_to = self._reply_to.get("message_id")
            references = build_reply_references(
                in_reply_to,
                self._reply_to.get("references"),
            )

        account = self._selected_account()
        if not account.can_send:
            self._show_error("This account has no mail transport configured")
            return

        self._sending = True
        self._set_compose_actions_sensitive(False)
        self._set_status("Sending message…")

        account_uid = account.uid
        draft_folder = self._draft_folder_name
        draft_uid = self._draft_message_uid
        attachments = list(self._attachments)

        def worker() -> None:
            error: Exception | None = None
            try:
                self._mail.send_message(
                    account_uid,
                    to=to_addrs,
                    cc=cc_addrs or None,
                    bcc=bcc_addrs or None,
                    subject=subject,
                    body=body,
                    in_reply_to=in_reply_to,
                    references=references,
                    attachments=attachments or None,
                )
            except SendQueued as exc:
                if self._on_outbox_changed is not None:
                    GLib.idle_add(self._on_outbox_changed)
                GLib.idle_add(
                    self._on_send_finished, None, exc.user_message, None, None
                )
                return
            except Exception as exc:
                log.warning("Send failed: %s", user_send_error_message(exc))
                error = exc
                GLib.idle_add(
                    self._on_send_finished,
                    error,
                    None,
                    None,
                    None,
                )
                return
            GLib.idle_add(
                self._on_send_finished,
                error,
                None,
                draft_folder,
                draft_uid,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_send_finished(
        self,
        error: Exception | None,
        success_status: str | None = None,
        draft_folder: str | None = None,
        draft_uid: str | None = None,
    ) -> bool:
        self._sending = False
        self._set_compose_actions_sensitive(True)
        if error is not None:
            message = user_send_error_message(error)
            self._show_error(message)
            return False

        if draft_folder and draft_uid:
            account = self._selected_account()

            def delete_worker() -> None:
                try:
                    self._mail.delete_draft(
                        account.uid, draft_folder, draft_uid
                    )
                except Exception:
                    log.warning(
                        "Could not delete draft %s in %r after send",
                        draft_uid,
                        draft_folder,
                        exc_info=True,
                    )
                GLib.idle_add(self._finish_send_after_draft_delete, success_status)

            threading.Thread(target=delete_worker, daemon=True).start()
            return False

        self._finish_send_after_draft_delete(success_status)
        return False

    def _finish_send_after_draft_delete(
        self, success_status: str | None = None
    ) -> bool:
        if self._on_draft_saved is not None:
            self._on_draft_saved()
        self._set_status(success_status or "Message sent")
        self._dismiss()
        return False

    def _show_error(self, message: str) -> None:
        show_error_toast(self, message)
