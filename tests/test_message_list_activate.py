# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.message_list_activate import (
    MessageListActivateAction,
    message_list_activate_action,
)


class MessageListActivateActionTests(unittest.TestCase):
    def test_drafts_folder_opens_compose(self) -> None:
        action = message_list_activate_action(
            is_drafts_folder=True,
            is_outbox_folder=False,
        )
        self.assertEqual(action, MessageListActivateAction.DRAFT_COMPOSE)

    def test_outbox_folder_opens_edit(self) -> None:
        action = message_list_activate_action(
            is_drafts_folder=False,
            is_outbox_folder=True,
        )
        self.assertEqual(action, MessageListActivateAction.OUTBOX_EDIT)

    def test_normal_folder_opens_reader_window(self) -> None:
        action = message_list_activate_action(
            is_drafts_folder=False,
            is_outbox_folder=False,
        )
        self.assertEqual(action, MessageListActivateAction.READER_WINDOW)

    def test_drafts_takes_precedence_over_outbox(self) -> None:
        action = message_list_activate_action(
            is_drafts_folder=True,
            is_outbox_folder=True,
        )
        self.assertEqual(action, MessageListActivateAction.DRAFT_COMPOSE)
