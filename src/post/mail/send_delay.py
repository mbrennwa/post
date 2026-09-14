# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Schedule delayed delivery of outbox messages."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from .send_queue import (
    clear_outbound_send_delay,
    clear_outbound_send_error,
    is_outbound_parked,
    is_outbound_ready_to_send,
    list_queued_outbound_messages,
    load_queued_outbound_message,
    park_outbound_message,
)
from .send_errors import (
    SendQueued,
    format_outbox_failure_toast,
    is_permanent_send_error,
    user_send_error_message,
)
from .network_errors import is_sign_in_required_error

if TYPE_CHECKING:
    from .eds import MailService

log = logging.getLogger(__name__)


class OutboundSendDelayScheduler:
    """Fire GLib timers for delayed outbox sends."""

    def __init__(
        self,
        mail: MailService,
        *,
        on_outbox_changed: Callable[[], None] | None = None,
        on_send_error: Callable[[str], None] | None = None,
    ) -> None:
        self._mail = mail
        self._on_outbox_changed = on_outbox_changed
        self._on_send_error = on_send_error
        self._timer_ids: dict[str, int] = {}

    def reschedule_all(self) -> None:
        self.cancel_all()
        now = time.time()
        for queue_id, message in list_queued_outbound_messages():
            if is_outbound_parked(message):
                continue
            if message.send_after is None or message.send_after <= now:
                continue
            self.schedule_item(queue_id, message.send_after)

    def schedule_item(self, queue_id: str, send_after: float) -> None:
        self.cancel(queue_id)
        delay_ms = max(0, int((send_after - time.time()) * 1000))
        if delay_ms == 0:
            self._mail.submit_interactive(
                "deliver_outbound", self._deliver_worker, queue_id
            )
            return
        self._timer_ids[queue_id] = GLib.timeout_add(
            delay_ms, self._on_timer_fired, queue_id
        )

    def cancel(self, queue_id: str) -> None:
        timer_id = self._timer_ids.pop(queue_id, None)
        if timer_id is not None:
            GLib.source_remove(timer_id)

    def send_now(self, queue_id: str) -> None:
        """Cancel send delay and deliver one outbox item immediately."""
        self.cancel(queue_id)
        self._mail.submit_interactive(
            "send_now_outbound", self._send_now_worker, queue_id
        )

    def cancel_all(self) -> None:
        for timer_id in self._timer_ids.values():
            GLib.source_remove(timer_id)
        self._timer_ids.clear()

    def _on_timer_fired(self, queue_id: str) -> bool:
        self._timer_ids.pop(queue_id, None)
        self._mail.submit_interactive(
            "deliver_outbound", self._deliver_worker, queue_id
        )
        return False

    def _send_now_worker(self, queue_id: str) -> None:
        try:
            clear_outbound_send_delay(queue_id)
            clear_outbound_send_error(queue_id)
        except FileNotFoundError:
            return
        except Exception:
            log.exception("Could not clear send delay for outbox item %s", queue_id)
            return
        self._deliver_worker(queue_id)

    def _deliver_worker(self, queue_id: str) -> None:
        try:
            message = load_queued_outbound_message(queue_id)
            if is_outbound_parked(message):
                return
            if not is_outbound_ready_to_send(message):
                if message.send_after is not None:
                    GLib.idle_add(self.schedule_item, queue_id, message.send_after)
                return
            self._mail.begin_outbound_send()
            try:
                self._mail.deliver_outbound_queue_item(queue_id)
            finally:
                self._mail.end_outbound_send()
        except FileNotFoundError:
            pass
        except SendQueued:
            log.debug("Delayed send deferred for outbox item %s", queue_id)
        except Exception as exc:
            if is_sign_in_required_error(exc):
                log.warning(
                    "Delayed send failed for outbox item %s: sign-in required",
                    queue_id,
                )
                try:
                    queued = load_queued_outbound_message(queue_id)
                except FileNotFoundError:
                    queued = None
                if queued is not None:
                    self._mail.set_account_connect_health(
                        queued.account_uid, "needs_sign_in"
                    )
            elif is_permanent_send_error(exc):
                reason = user_send_error_message(exc)
                try:
                    park_outbound_message(queue_id, reason)
                except FileNotFoundError:
                    pass
                else:
                    log.warning(
                        "Delayed send failed for outbox item %s: %s",
                        queue_id,
                        reason,
                    )
                    self._notify_send_error_idle(
                        format_outbox_failure_toast(
                            reason,
                            subject=message.subject,
                            to=message.to,
                        )
                    )
            else:
                log.exception("Delayed send failed for outbox item %s", queue_id)
        if self._on_outbox_changed is not None:
            GLib.idle_add(self._notify_outbox_changed)

    def _notify_send_error_idle(self, message: str) -> None:
        if self._on_send_error is None:
            return
        GLib.idle_add(self._notify_send_error, message)

    def _notify_send_error(self, message: str) -> bool:
        if self._on_send_error is not None:
            self._on_send_error(message)
        return False

    def _notify_outbox_changed(self) -> bool:
        if self._on_outbox_changed is not None:
            self._on_outbox_changed()
        return False
