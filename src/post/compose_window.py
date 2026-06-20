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
    build_reply_references,
    build_reply_subject,
    extract_reply_address,
    parse_address_list,
    quote_plain_reply,
)
from post.mail.eds import MailAccount

log = logging.getLogger(__name__)

ComposeMode = Literal["new", "reply"]
SetStatus = Callable[[str], None]


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
        self._account = account
        self._set_status = set_status
        self._mode = mode
        self._reply_to = reply_to
        self._sending = False

        title = "Reply" if mode == "reply" else "New Message"
        self.set_title(title)
        self.set_default_size(720, 560)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        outer.append(header)

        self._send_btn = Gtk.Button(label="Send")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.connect("clicked", self._on_send_clicked)
        header.pack_end(self._send_btn)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        outer.append(scrolled)

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

        form.append(self._labeled_row("From", Gtk.Label(label=account.from_label, xalign=0)))

        self._to_entry = Gtk.Entry()
        self._to_entry.set_placeholder_text("recipient@example.com")
        form.append(self._labeled_row("To", self._to_entry))

        self._extra_revealer = Gtk.Revealer()
        self._extra_revealer.set_reveal_child(False)
        self._extra_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        extra_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._cc_entry = Gtk.Entry()
        self._cc_entry.set_placeholder_text("Optional")
        extra_box.append(self._labeled_row("Cc", self._cc_entry))
        self._bcc_entry = Gtk.Entry()
        self._bcc_entry.set_placeholder_text("Optional")
        extra_box.append(self._labeled_row("Bcc", self._bcc_entry))
        self._extra_revealer.set_child(extra_box)
        form.append(self._extra_revealer)

        show_extra_btn = Gtk.Button(label="Cc / Bcc")
        show_extra_btn.add_css_class("flat")
        show_extra_btn.set_halign(Gtk.Align.START)
        show_extra_btn.connect("clicked", self._on_toggle_extra)
        form.append(show_extra_btn)

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
        form.append(self._labeled_row("Body", body_frame))

        self._prefill_fields()

    @staticmethod
    def _labeled_row(label: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label=label, xalign=0)
        heading.add_css_class("heading")
        row.append(heading)
        row.append(widget)
        return row

    def _prefill_fields(self) -> None:
        if self._mode == "reply" and self._reply_to is not None:
            try:
                self._to_entry.set_text(extract_reply_address(self._reply_to.get("from", "")))
            except ValueError as exc:
                self._show_error(str(exc))
            self._subject_entry.set_text(
                build_reply_subject(self._reply_to.get("subject") or "")
            )
            body = quote_plain_reply(
                self._reply_to,
                self._reply_to.get("body_plain"),
            )
            self._body_view.get_buffer().set_text(body)
        else:
            self._body_view.get_buffer().set_text("")

    def _on_toggle_extra(self, button: Gtk.Button) -> None:
        reveal = not self._extra_revealer.get_reveal_child()
        self._extra_revealer.set_reveal_child(reveal)
        button.set_label("Hide Cc / Bcc" if reveal else "Cc / Bcc")

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
        if self._mode == "reply" and self._reply_to is not None:
            in_reply_to = self._reply_to.get("message_id")
            references = build_reply_references(
                in_reply_to,
                self._reply_to.get("references"),
            )

        self._sending = True
        self._send_btn.set_sensitive(False)
        self._set_status("Sending message…")

        account_uid = self._account.uid

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
