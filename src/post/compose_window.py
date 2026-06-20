# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Compose window for new messages and replies."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Literal

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk

from post.mail import MailService
from post.mail.compose import (
    body_is_unedited_signature_template,
    build_forward_subject,
    build_reply_all_recipients,
    build_reply_references,
    build_reply_subject,
    compose_body_with_signature,
    extract_reply_address,
    format_address_list,
    normalize_email,
    parse_address_list,
    quote_plain_forward,
    quote_plain_reply,
)
from post.mail.correspondents import (
    Correspondent,
    apply_address_completion,
    current_address_token,
    match_correspondents,
)
from post.mail.eds import MailAccount
from post.preferences import get_account_signature, get_account_signatures

log = logging.getLogger(__name__)

ComposeMode = Literal["new", "reply", "reply-all", "forward"]
SetStatus = Callable[[str], None]

_LABEL_WIDTH = 72


class ComposeWindow(Adw.Window):
    def __init__(
        self,
        *,
        parent: Gtk.Window,
        mail: MailService,
        account: MailAccount,
        set_status: SetStatus,
        mode: ComposeMode = "new",
        reply_to: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(transient_for=parent, modal=False)
        self._mail = mail
        self._set_status = set_status
        self._mode = mode
        self._reply_to = reply_to
        self._sending = False
        self._send_accounts = mail.list_sendable_accounts()
        if not self._send_accounts:
            raise ValueError("No mail account configured for sending")
        self._account = account if account.can_send else self._send_accounts[0]

        if mode == "reply-all":
            title = "Reply All"
        elif mode == "reply":
            title = "Reply"
        elif mode == "forward":
            title = "Forward"
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

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.connect("clicked", self._on_send_clicked)
        header.pack_end(self._send_btn)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form.set_margin_start(18)
        form.set_margin_end(18)
        form.set_margin_top(12)
        form.set_margin_bottom(12)
        scrolled.set_child(form)

        self._error_label = Gtk.Label(label="", xalign=0, wrap=True)
        self._error_label.add_css_class("error")
        self._error_label.set_visible(False)
        form.append(self._error_label)

        from_labels = [send_account.from_label for send_account in self._send_accounts]
        self._from_dropdown = Gtk.DropDown.new_from_strings(from_labels)
        self._from_dropdown.set_hexpand(True)
        for index, send_account in enumerate(self._send_accounts):
            if send_account.uid == self._account.uid:
                self._from_dropdown.set_selected(index)
                break
        if len(self._send_accounts) == 1:
            self._from_dropdown.set_sensitive(False)
        self._from_dropdown.connect("notify::selected", self._on_from_account_changed)
        form.append(self._labeled_row("From", self._from_dropdown))

        self._correspondents: list[Correspondent] = []
        self._correspondents_generation = 0
        self._completion_model = Gtk.ListStore(str)
        self._entry_match_cache: dict[int, tuple[str, int, frozenset[str]]] = {}

        self._to_entry = Gtk.Entry()
        self._to_entry.set_placeholder_text("recipient@example.com")
        form.append(self._labeled_row("To", self._to_entry))
        self._setup_address_completion(self._to_entry)

        self._cc_entry = Gtk.Entry()
        self._cc_entry.set_placeholder_text("Optional")
        self._cc_entry.set_hexpand(True)

        self._bcc_toggle_btn = Gtk.Button(label="Show Bcc")
        self._bcc_toggle_btn.add_css_class("flat")
        self._bcc_toggle_btn.connect("clicked", self._on_toggle_bcc)

        self._cc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._cc_row.append(self._field_label("Cc"))
        self._cc_row.append(self._cc_entry)
        self._cc_row.append(self._bcc_toggle_btn)
        form.append(self._cc_row)
        self._setup_address_completion(self._cc_entry)

        self._bcc_entry = Gtk.Entry()
        self._bcc_entry.set_placeholder_text("Optional")
        self._bcc_entry.set_hexpand(True)
        self._bcc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._bcc_row.append(self._field_label("Bcc"))
        self._bcc_row.append(self._bcc_entry)
        self._bcc_row.set_visible(False)
        form.append(self._bcc_row)
        self._setup_address_completion(self._bcc_entry)

        self._subject_entry = Gtk.Entry()
        form.append(self._labeled_row("Subject", self._subject_entry))

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
        body_scroll.set_child(self._body_view)
        body_frame.set_child(body_scroll)
        body_frame.set_vexpand(True)
        form.append(body_frame)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

        self._prefill_fields()
        self._load_correspondents()
        GLib.idle_add(self._set_initial_focus)

    def _set_initial_focus(self) -> bool:
        if self._mode == "new":
            self._to_entry.grab_focus()
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
        self._load_correspondents()
        if self._mode == "new":
            self._refresh_signature_for_account(self._selected_account())

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
        buffer.set_text(body)

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
            except Exception:
                log.exception(
                    "Failed to load correspondents for account %s", account_uid
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
        return self._send_accounts[index]

    def _own_addresses(self) -> set[str]:
        account = self._selected_account()
        own: set[str] = set()
        for raw in (account.from_address, account.email):
            if raw:
                own.add(normalize_email(raw))
        return own

    def _prefill_fields(self) -> None:
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
        else:
            body = compose_body_with_signature(
                mode="new",
                quoted_body="",
                signature=signature,
            )
            self._body_view.get_buffer().set_text(body)

    def _on_toggle_bcc(self, button: Gtk.Button) -> None:
        reveal = not self._bcc_row.get_visible()
        self._bcc_row.set_visible(reveal)
        if reveal:
            button.set_label("Hide Bcc")
            self._cc_row.remove(button)
            self._bcc_row.append(button)
        else:
            button.set_label("Show Bcc")
            self._bcc_row.remove(button)
            self._cc_row.append(button)

    def _on_cancel_clicked(self, *_args) -> None:
        self.close()

    def _on_send_clicked(self, *_args) -> None:
        if self._sending:
            return

        self._error_label.set_visible(False)
        try:
            to_addrs = parse_address_list(self._to_entry.get_text())
            cc_addrs = parse_address_list(self._cc_entry.get_text())
            bcc_addrs = parse_address_list(self._bcc_entry.get_text())
        except ValueError as exc:
            self._show_error(str(exc))
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

        self._sending = True
        self._send_btn.set_sensitive(False)
        self._set_status("Sending message…")

        account = self._selected_account()
        account_uid = account.uid

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
                )
            except Exception as exc:
                log.exception("Failed to send message")
                error = exc
            GLib.idle_add(
                self._on_send_finished,
                error,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_send_finished(self, error: Exception | None) -> bool:
        self._sending = False
        self._send_btn.set_sensitive(True)
        if error is not None:
            self._show_error(str(error))
            self._set_status(f"Send failed: {error}")
            return False
        self._set_status("Message sent")
        self.close()
        return False

    def _show_error(self, message: str) -> None:
        self._error_label.set_label(message)
        self._error_label.set_visible(True)
