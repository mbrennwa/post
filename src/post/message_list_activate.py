# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Route message list activation (double-click / Enter) to the correct action."""

from __future__ import annotations

from enum import Enum


class MessageListActivateAction(Enum):
    DRAFT_COMPOSE = "draft_compose"
    OUTBOX_EDIT = "outbox_edit"
    READER_WINDOW = "reader_window"


def message_list_activate_action(
    *,
    is_drafts_folder: bool,
    is_outbox_folder: bool,
) -> MessageListActivateAction:
    """Return which handler should run when a message row is activated."""
    if is_drafts_folder:
        return MessageListActivateAction.DRAFT_COMPOSE
    if is_outbox_folder:
        return MessageListActivateAction.OUTBOX_EDIT
    return MessageListActivateAction.READER_WINDOW
