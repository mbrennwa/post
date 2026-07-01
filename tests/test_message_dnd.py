# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.dnd import (
    MessageTransferPayload,
    decode_message_transfer,
    encode_message_transfer,
    validate_message_drop,
)


class MessageTransferPayloadTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        payload = MessageTransferPayload(
            account_uid="acct-1",
            source_folder="INBOX",
            uids=("1", "2"),
        )
        decoded = decode_message_transfer(encode_message_transfer(payload))
        self.assertEqual(decoded, payload)

    def test_decode_rejects_invalid_json(self) -> None:
        self.assertIsNone(decode_message_transfer(b"not-json"))

    def test_validate_rejects_cross_account(self) -> None:
        payload = MessageTransferPayload(
            account_uid="acct-1",
            source_folder="INBOX",
            uids=("1",),
        )
        self.assertFalse(
            validate_message_drop(
                payload,
                dest_account_uid="acct-2",
                dest_folder="Archive",
                dest_is_outbox=False,
            )
        )

    def test_validate_rejects_same_folder(self) -> None:
        payload = MessageTransferPayload(
            account_uid="acct-1",
            source_folder="INBOX",
            uids=("1",),
        )
        self.assertFalse(
            validate_message_drop(
                payload,
                dest_account_uid="acct-1",
                dest_folder="INBOX",
                dest_is_outbox=False,
            )
        )

    def test_validate_rejects_outbox(self) -> None:
        payload = MessageTransferPayload(
            account_uid="acct-1",
            source_folder="INBOX",
            uids=("1",),
        )
        self.assertFalse(
            validate_message_drop(
                payload,
                dest_account_uid="acct-1",
                dest_folder="Outbox",
                dest_is_outbox=True,
            )
        )

    def test_validate_accepts_valid_drop(self) -> None:
        payload = MessageTransferPayload(
            account_uid="acct-1",
            source_folder="INBOX",
            uids=("1",),
        )
        self.assertTrue(
            validate_message_drop(
                payload,
                dest_account_uid="acct-1",
                dest_folder="Archive",
                dest_is_outbox=False,
            )
        )
