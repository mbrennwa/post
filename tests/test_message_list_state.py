# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from collections import OrderedDict

from post.mail.message_list_state import (
    FolderListSnapshot,
    folder_cache_matches,
    folder_list_ready_to_cache,
    message_list_fingerprint,
    prepended_message_count,
    touch_lru_cache,
)


def _msg(uid: str) -> dict:
    return {"uid": uid, "subject": f"Message {uid}"}


class MessageListFingerprintTests(unittest.TestCase):
    def test_empty_list(self) -> None:
        self.assertEqual(message_list_fingerprint([]), ())

    def test_preserves_order(self) -> None:
        messages = [_msg("3"), _msg("1"), _msg("2")]
        self.assertEqual(
            message_list_fingerprint(messages),
            ("3:Message 3", "1:Message 1", "2:Message 2"),
        )

    def test_subject_change_is_detected(self) -> None:
        first = [{"uid": "1", "subject": "Old subject"}]
        second = [{"uid": "1", "subject": "New subject"}]
        self.assertNotEqual(
            message_list_fingerprint(first),
            message_list_fingerprint(second),
        )

    def test_order_change_is_detected(self) -> None:
        first = [_msg("1"), _msg("2")]
        second = [_msg("2"), _msg("1")]
        self.assertNotEqual(
            message_list_fingerprint(first),
            message_list_fingerprint(second),
        )


class PrependedMessageCountTests(unittest.TestCase):
    def test_detects_single_prepend(self) -> None:
        current = [_msg("2"), _msg("1")]
        updated = [_msg("3"), _msg("2"), _msg("1")]
        self.assertEqual(prepended_message_count(current, updated), 1)

    def test_detects_multiple_prepends(self) -> None:
        current = [_msg("2"), _msg("1")]
        updated = [_msg("4"), _msg("3"), _msg("2"), _msg("1")]
        self.assertEqual(prepended_message_count(current, updated), 2)

    def test_rejects_reorder(self) -> None:
        current = [_msg("2"), _msg("1")]
        updated = [_msg("1"), _msg("2")]
        self.assertEqual(prepended_message_count(current, updated), 0)

    def test_rejects_removal(self) -> None:
        current = [_msg("2"), _msg("1")]
        updated = [_msg("2")]
        self.assertEqual(prepended_message_count(current, updated), 0)

    def test_empty_current_treats_all_as_prepended(self) -> None:
        updated = [_msg("1"), _msg("2")]
        self.assertEqual(prepended_message_count([], updated), 2)


class FolderListReadyToCacheTests(unittest.TestCase):
    def test_not_ready_while_populating(self) -> None:
        self.assertFalse(
            folder_list_ready_to_cache(10, 20, True, [_msg("1")])
        )

    def test_not_ready_without_messages(self) -> None:
        self.assertFalse(folder_list_ready_to_cache(0, 10, False, None))

    def test_not_ready_when_partial(self) -> None:
        messages = [_msg("1"), _msg("2")]
        self.assertFalse(
            folder_list_ready_to_cache(1, 2, False, messages)
        )

    def test_ready_when_fully_shown(self) -> None:
        messages = [_msg("1"), _msg("2")]
        self.assertTrue(
            folder_list_ready_to_cache(2, 2, False, messages)
        )

    def test_ready_for_empty_folder(self) -> None:
        self.assertTrue(folder_list_ready_to_cache(0, 0, False, []))


class FolderCacheMatchesTests(unittest.TestCase):
    def test_matches_when_fingerprint_and_total_agree(self) -> None:
        messages = [_msg("1"), _msg("2")]
        fingerprint = message_list_fingerprint(messages)
        self.assertTrue(folder_cache_matches(messages, 2, fingerprint))

    def test_rejects_total_mismatch(self) -> None:
        messages = [_msg("1"), _msg("2")]
        fingerprint = message_list_fingerprint(messages)
        self.assertFalse(folder_cache_matches(messages, 3, fingerprint))

    def test_rejects_uid_mismatch(self) -> None:
        messages = [_msg("1"), _msg("2")]
        fingerprint = message_list_fingerprint(messages)
        changed = [_msg("1"), _msg("3")]
        self.assertFalse(folder_cache_matches(changed, 2, fingerprint))


class LruCacheTests(unittest.TestCase):
    def test_touch_moves_entry_to_most_recent(self) -> None:
        cache: OrderedDict[str, str] = OrderedDict()
        touch_lru_cache(cache, "a", "1", max_size=2)
        touch_lru_cache(cache, "b", "2", max_size=2)
        touch_lru_cache(cache, "a", "1-updated", max_size=2)
        touch_lru_cache(cache, "c", "3", max_size=2)
        self.assertEqual(list(cache.keys()), ["a", "c"])
        self.assertEqual(cache["a"], "1-updated")

    def test_snapshot_dataclass_fields(self) -> None:
        messages = [_msg("1")]
        snapshot = FolderListSnapshot(
            rows=[],
            fingerprint=message_list_fingerprint(messages),
            messages=messages,
            shown_count=1,
            total=1,
            source="memory",
            scroll_value=12.5,
            selected_uid="1",
        )
        self.assertEqual(snapshot.selected_uid, "1")
        self.assertEqual(snapshot.source, "memory")


if __name__ == "__main__":
    unittest.main()
