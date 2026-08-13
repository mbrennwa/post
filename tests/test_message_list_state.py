# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from collections import OrderedDict

from post.mail.message_list_state import (
    FolderListSnapshot,
    MESSAGE_LIST_UI_BATCH_SIZE,
    dedupe_folder_index_messages,
    folder_cache_matches,
    folder_index_covers_identities,
    folder_list_ready_to_cache,
    message_batch_ranges,
    message_list_fingerprint,
    normalize_folder_index_by_uid,
    prepended_message_count,
    prune_stale_folder_index_uids,
    touch_lru_cache,
    upsert_folder_index_by_identity,
    find_folder_index_sibling_uids,
    _folder_message_identity_key,
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


class MessageBatchRangesTests(unittest.TestCase):
    def test_batch_size_constant(self) -> None:
        self.assertEqual(MESSAGE_LIST_UI_BATCH_SIZE, 100)

    def test_bind_cap_constant(self) -> None:
        from post.mail.message_list_state import (
            MESSAGE_LIST_UI_BIND_CAP,
            MESSAGE_LIST_UI_BIND_MORE,
        )

        self.assertEqual(MESSAGE_LIST_UI_BIND_CAP, 500)
        self.assertEqual(MESSAGE_LIST_UI_BIND_MORE, 500)

    def test_heavy_folder_name(self) -> None:
        from post.mail.message_list_state import is_heavy_folder_name

        self.assertTrue(is_heavy_folder_name("Archive"))
        self.assertTrue(is_heavy_folder_name("INBOX/Archive"))
        self.assertTrue(is_heavy_folder_name("[Google Mail]/All Mail"))
        self.assertTrue(is_heavy_folder_name("Trash"))
        self.assertTrue(is_heavy_folder_name("Deleted Items"))
        self.assertTrue(is_heavy_folder_name("Junk"))
        self.assertTrue(is_heavy_folder_name("Spam"))
        self.assertFalse(is_heavy_folder_name("Inbox"))
        self.assertFalse(is_heavy_folder_name("Sent Items"))

    def test_trash_or_junk_folder_name(self) -> None:
        from post.mail.message_list_state import (
            is_archive_folder_name,
            is_trash_or_junk_folder_name,
        )

        self.assertTrue(is_trash_or_junk_folder_name("Spam"))
        self.assertTrue(is_trash_or_junk_folder_name("Junk Email"))
        self.assertTrue(is_trash_or_junk_folder_name("Deleted Items"))
        self.assertFalse(is_trash_or_junk_folder_name("Archive"))
        self.assertTrue(is_archive_folder_name("Archive"))
        self.assertFalse(is_archive_folder_name("Spam"))

    def test_offline_folder_priority_order(self) -> None:
        from post.mail.message_list_state import (
            OFFLINE_PRIORITY_ARCHIVE,
            OFFLINE_PRIORITY_JUNK,
            OFFLINE_PRIORITY_ORDINARY,
            OFFLINE_PRIORITY_TRASH,
            offline_folder_priority,
        )

        self.assertEqual(offline_folder_priority("INBOX"), OFFLINE_PRIORITY_ORDINARY)
        self.assertEqual(offline_folder_priority("Archive"), OFFLINE_PRIORITY_ARCHIVE)
        self.assertEqual(offline_folder_priority("Trash"), OFFLINE_PRIORITY_TRASH)
        self.assertEqual(offline_folder_priority("Junk"), OFFLINE_PRIORITY_JUNK)
        self.assertLess(OFFLINE_PRIORITY_ORDINARY, OFFLINE_PRIORITY_ARCHIVE)
        self.assertLess(OFFLINE_PRIORITY_ARCHIVE, OFFLINE_PRIORITY_TRASH)
        self.assertLess(OFFLINE_PRIORITY_TRASH, OFFLINE_PRIORITY_JUNK)

        # Camel flags win over ambiguous names when provided.
        self.assertEqual(
            offline_folder_priority(
                "Custom",
                folder_flags=4096,
                type_junk=4096,
            ),
            OFFLINE_PRIORITY_JUNK,
        )

    def test_lists_equivalent_samples_large_lists(self) -> None:
        from post.mail.message_list_state import message_lists_equivalent_for_ui

        current = [{"uid": str(i), "subject": f"s{i}"} for i in range(200)]
        refreshed = [dict(m) for m in current]
        self.assertTrue(
            message_lists_equivalent_for_ui(
                current,
                refreshed,
                current_total=200,
                refreshed_total=200,
            )
        )
        refreshed[0]["subject"] = "changed"
        self.assertFalse(
            message_lists_equivalent_for_ui(
                current,
                refreshed,
                current_total=200,
                refreshed_total=200,
            )
        )

    def test_message_flag_patches_detects_seen_and_flagged(self) -> None:
        from post.mail.message_list_state import (
            message_flag_patches,
            message_lists_equivalent_for_ui,
        )

        current = [
            {
                "uid": "1",
                "subject": "A",
                "flags": {"seen": False, "flagged": False},
            },
            {
                "uid": "2",
                "subject": "B",
                "flags": {"seen": True, "flagged": False},
            },
        ]
        refreshed = [
            {
                "uid": "1",
                "subject": "A",
                "flags": {"seen": True, "flagged": True},
            },
            {
                "uid": "2",
                "subject": "B",
                "flags": {"seen": True, "flagged": False},
            },
        ]
        self.assertTrue(
            message_lists_equivalent_for_ui(
                current,
                refreshed,
                current_total=2,
                refreshed_total=2,
            )
        )
        self.assertEqual(
            message_flag_patches(current, refreshed),
            [("1", {"seen": True, "flagged": True})],
        )


    def test_empty(self) -> None:
        self.assertEqual(message_batch_ranges(0), [])

    def test_single_batch(self) -> None:
        self.assertEqual(message_batch_ranges(100, batch_size=500), [(0, 100)])

    def test_multiple_batches(self) -> None:
        self.assertEqual(
            message_batch_ranges(1200, batch_size=500),
            [(0, 500), (500, 1000), (1000, 1200)],
        )


class DedupeFolderIndexMessagesTests(unittest.TestCase):
    def test_prefers_camel_uid_for_same_message_id(self) -> None:
        stale = {
            "uid": "old-rest-id",
            "message_id": "<msg@example.com>",
            "subject": "Hello",
            "from": "a@b.c",
            "sort_date": 100,
        }
        live = {
            "uid": "new-rest-id",
            "message_id": "<msg@example.com>",
            "subject": "Hello",
            "from": "a@b.c",
            "sort_date": 100,
        }
        deduped = dedupe_folder_index_messages(
            [stale, live],
            prefer_uids={"new-rest-id"},
        )
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["uid"], "new-rest-id")

    def test_prefers_last_uid_when_camel_set_empty(self) -> None:
        stale = {
            "uid": "old-rest-id",
            "message_id": "<msg@example.com>",
            "subject": "Hello",
            "from": "a@b.c",
            "sort_date": 100,
        }
        live = {
            "uid": "new-rest-id",
            "message_id": "<msg@example.com>",
            "subject": "Hello",
            "from": "a@b.c",
            "sort_date": 100,
        }
        deduped = dedupe_folder_index_messages([stale, live], prefer_uids=set())
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["uid"], "new-rest-id")

    def test_covers_identities_after_duplicate_collapse(self) -> None:
        existing = [
            {
                "uid": "old",
                "message_id": "<a@x>",
                "subject": "A",
                "from": "a",
                "sort_date": 1,
            },
            {
                "uid": "new",
                "message_id": "<a@x>",
                "subject": "A",
                "from": "a",
                "sort_date": 1,
            },
        ]
        incoming = [
            {
                "uid": "new",
                "message_id": "<a@x>",
                "subject": "A",
                "from": "a",
                "sort_date": 1,
            },
        ]
        self.assertTrue(folder_index_covers_identities(incoming, existing))

    def test_refuses_partial_summary_as_cover(self) -> None:
        existing = [
            {
                "uid": "1",
                "message_id": "<a@x>",
                "subject": "A",
                "from": "a",
                "sort_date": 1,
            },
            {
                "uid": "2",
                "message_id": "<b@x>",
                "subject": "B",
                "from": "b",
                "sort_date": 2,
            },
        ]
        incoming = [existing[0]]
        self.assertFalse(folder_index_covers_identities(incoming, existing))


class FolderIndexIdentityUpsertTests(unittest.TestCase):
    def test_rfc_message_id_identity(self) -> None:
        self.assertEqual(
            _folder_message_identity_key(
                {"uid": "1", "message_id": "<Msg@Example.COM>"}
            ),
            "mid:msg@example.com",
        )

    def test_hash_zero_does_not_merge(self) -> None:
        a = {"uid": "a", "message_id": "0", "subject": "Same", "from": "x"}
        b = {"uid": "b", "message_id_hash": 0, "subject": "Same", "from": "x"}
        self.assertTrue(_folder_message_identity_key(a).startswith("uid:"))
        self.assertTrue(_folder_message_identity_key(b).startswith("uid:"))
        self.assertNotEqual(
            _folder_message_identity_key(a),
            _folder_message_identity_key(b),
        )

    def test_legacy_decimal_hash_merges(self) -> None:
        stale = {"uid": "old", "message_id": "424242"}
        live = {"uid": "new", "message_id_hash": 424242}
        self.assertEqual(
            _folder_message_identity_key(stale),
            _folder_message_identity_key(live),
        )

    def test_missing_mid_stays_uid_distinct(self) -> None:
        a = {
            "uid": "1",
            "subject": "Newsletter",
            "from": "news@x",
            "sort_date": 1,
        }
        b = {
            "uid": "2",
            "subject": "Newsletter",
            "from": "news@x",
            "sort_date": 1,
        }
        self.assertNotEqual(
            _folder_message_identity_key(a),
            _folder_message_identity_key(b),
        )

    def test_sibling_uids_prefer_live(self) -> None:
        messages = [
            {
                "uid": "stale-uid",
                "message_id": "<msg@example.com>",
                "subject": "Hello",
            },
            {
                "uid": "other",
                "message_id": "<other@example.com>",
            },
            {
                "uid": "live-uid",
                "message_id": "<msg@example.com>",
                "subject": "Hello",
            },
        ]
        self.assertEqual(
            find_folder_index_sibling_uids(
                messages, "stale-uid", prefer_uids={"live-uid"}
            ),
            ["live-uid"],
        )

    def test_sibling_uids_skip_uid_identity(self) -> None:
        messages = [
            {"uid": "src-1", "subject": "Hi", "from": "a", "sort_date": 1},
            {"uid": "src-2", "subject": "Hi", "from": "a", "sort_date": 1},
        ]
        self.assertEqual(
            find_folder_index_sibling_uids(messages, "src-1"),
            [],
        )

    def test_upsert_replaces_stale_restid(self) -> None:
        by_uid = {
            "old-rest-id": {
                "uid": "old-rest-id",
                "message_id": "<msg@example.com>",
                "subject": "Hello",
            }
        }
        replaced = upsert_folder_index_by_identity(
            by_uid,
            {
                "uid": "new-rest-id",
                "message_id": "<msg@example.com>",
                "subject": "Hello",
            },
            prefer_uids={"new-rest-id"},
        )
        self.assertEqual(replaced, "old-rest-id")
        self.assertEqual(set(by_uid), {"new-rest-id"})
        self.assertEqual(by_uid["new-rest-id"]["uid"], "new-rest-id")

    def test_normalize_collapses_seed_duplicates(self) -> None:
        messages = [
            {
                "uid": "old",
                "message_id": "<a@x>",
                "subject": "A",
                "sort_date": 1,
            },
            {
                "uid": "new",
                "message_id": "<a@x>",
                "subject": "A",
                "sort_date": 1,
            },
        ]
        by_uid = normalize_folder_index_by_uid(
            messages, prefer_uids={"new"}
        )
        self.assertEqual(set(by_uid), {"new"})

    def test_prune_drops_stale_when_alternate_live(self) -> None:
        by_uid = {
            "old": {"uid": "old", "message_id": "<a@x>"},
            "new": {"uid": "new", "message_id": "<a@x>"},
            "other": {"uid": "other", "message_id": "<b@x>"},
        }
        removed = prune_stale_folder_index_uids(by_uid, {"new", "other"})
        self.assertEqual(removed, ["old"])
        self.assertEqual(set(by_uid), {"new", "other"})

    def test_prune_keeps_unique_when_camel_partial(self) -> None:
        by_uid = {
            "only": {"uid": "only", "message_id": "<a@x>"},
        }
        removed = prune_stale_folder_index_uids(by_uid, {"something-else"})
        self.assertEqual(removed, [])
        self.assertIn("only", by_uid)

    def test_normalize_large_index_is_linear(self) -> None:
        # Regression: O(n²) identity scan froze Archive open (#267).
        import time

        messages = [
            {
                "uid": f"uid-{i}",
                "message_id": f"<msg-{i}@example.com>",
                "subject": f"S{i}",
                "sort_date": i,
            }
            for i in range(8_000)
        ]
        # Inject a few RestId duplicates that must collapse.
        messages.append(
            {
                "uid": "uid-dup",
                "message_id": "<msg-1@example.com>",
                "subject": "S1",
                "sort_date": 1,
            }
        )
        started = time.perf_counter()
        by_uid = normalize_folder_index_by_uid(
            messages, prefer_uids={"uid-1"}
        )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(by_uid), 8_000)
        self.assertIn("uid-1", by_uid)
        self.assertNotIn("uid-dup", by_uid)


if __name__ == "__main__":
    unittest.main()
