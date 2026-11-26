# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Automated soak for composer address ranking (#544).

Hammers the correspondent index the way a long session does: the same folders
stored over and over, folders growing, restarts that reload the disk cache, an
old v1 cache being migrated, several accounts side by side. Counts must never
drift and the suggestion order must stay stable.
"""

from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from post.mail import correspondent_cache, folder_index_cache
from post.mail.correspondents import (
    collect_correspondents,
    correspondent_score,
    match_correspondents,
)
from post.mail.eds import MailService, _FolderMessageIndex

NOW = 1_800_000_000
DAY = 24 * 3600

_TREE = [
    {"full_name": "INBOX", "display_name": "Inbox", "flags": 1024},
    {"full_name": "[Gmail]/Sent Mail", "display_name": "Sent Mail", "flags": 5120},
    {"full_name": "Archive", "display_name": "Archive", "flags": 0},
]


def _inbox(count: int = 60) -> list[dict]:
    messages: list[dict] = []
    for i in range(count):
        messages.append(
            {
                "uid": f"i{i}",
                "from": "Nina Nord <nina@work.example>",
                "to": "Me <me@gmail.example>",
                "cc": "Carol <carol@work.example>",
                "sort_date": NOW - (i + 1) * DAY,
            }
        )
    # A newsletter that wrote very recently, many times.
    for i in range(80):
        messages.append(
            {
                "uid": f"n{i}",
                "from": "Shop News <news@shop.example>",
                "to": "Me <me@gmail.example>",
                "sort_date": NOW - 60 - i,
            }
        )
    return messages


def _sent(count: int = 25) -> list[dict]:
    return [
        {
            "uid": f"s{i}",
            "from": "Me <me@gmail.example>",
            "to": "Nina Nord <nina@work.example>",
            "bcc": "Me <me@gmail.example>" if i % 5 == 0 else "",
            "sort_date": NOW - (i + 2) * DAY,
        }
        for i in range(count)
    ]


def _archive() -> list[dict]:
    return [
        {
            "uid": f"a{i}",
            "from": f"Old Friend {i % 7} <friend{i % 7}@old.example>",
            "sort_date": NOW - (400 + i) * DAY,
        }
        for i in range(70)
    ]


class CorrespondentSoak(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(
                folder_index_cache, "_CACHE_ROOT", root / "folder-index"
            ),
            mock.patch.object(
                correspondent_cache, "_CACHE_ROOT", root / "correspondents"
            ),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()

    def _service(self, *accounts: str) -> MailService:
        service = MailService(registry=mock.Mock())
        service._build_folder_index_unlocked = mock.Mock(
            side_effect=AssertionError("must not open folders for correspondents")
        )
        service._get_store_unlocked = mock.Mock(
            side_effect=AssertionError("must not connect for correspondents")
        )
        service._folder_tree_cache = {uid: list(_TREE) for uid in accounts}
        return service

    @staticmethod
    def _store(service: MailService, account: str, folder: str, messages) -> None:
        service._store_folder_index(
            account,
            folder,
            _FolderMessageIndex(
                messages=list(messages), unread=0, total=len(messages)
            ),
        )

    @staticmethod
    def _by_email(service: MailService, account: str) -> dict:
        return {
            item.email: item for item in service.get_correspondents_cached(account)
        }

    @staticmethod
    def _order(service: MailService, account: str, prefix: str) -> list[str]:
        return [
            item.email
            for item in match_correspondents(
                service.get_correspondents_cached(account), prefix, now=NOW
            )
        ]

    def test_repeated_stores_and_restarts_never_drift(self) -> None:
        accounts = ("acct-gmail", "acct-work", "acct-misc")
        folders = {
            "INBOX": _inbox(),
            "[Gmail]/Sent Mail": _sent(),
            "Archive": _archive(),
        }
        expected: dict[str, dict] = {}
        for name, messages in folders.items():
            sent = name == "[Gmail]/Sent Mail"
            for item in collect_correspondents(messages, is_sent_folder=sent):
                expected.setdefault(item.email, {"sent": 0, "received": 0})
                expected[item.email]["sent"] += item.sent_count
                expected[item.email]["received"] += item.received_count

        service = self._service(*accounts)
        for account in accounts:
            self._store(service, account, "INBOX", folders["INBOX"])
            service._get_correspondents_unlocked(account)

        rng = random.Random(544)
        baseline_order: dict[tuple[str, str], list[str]] = {}
        for cycle in range(150):
            if cycle and cycle % 25 == 0:
                # Restart: memory is gone, only the disk cache remains.
                service = self._service(*accounts)
                for account in accounts:
                    service._get_correspondents_unlocked(account)
            for account in accounts:
                names = list(folders)
                rng.shuffle(names)
                for name in names:
                    self._store(service, account, name, folders[name])
            if cycle == 0:
                for account in accounts:
                    for prefix in ("n", "me", "friend", "c", "@"):
                        baseline_order[(account, prefix)] = self._order(
                            service, account, prefix
                        )
            if cycle % 10 == 0:
                for account in accounts:
                    by_email = self._by_email(service, account)
                    for email, counts in expected.items():
                        self.assertEqual(
                            (
                                by_email[email].sent_count,
                                by_email[email].received_count,
                            ),
                            (counts["sent"], counts["received"]),
                            f"cycle {cycle} {account} {email}",
                        )
                    for (acct, prefix), order in baseline_order.items():
                        if acct == account:
                            self.assertEqual(
                                self._order(service, account, prefix),
                                order,
                                f"cycle {cycle} {account} prefix {prefix!r}",
                            )

    def test_growing_sent_folder_adds_exactly_one_per_message(self) -> None:
        service = self._service("acct-gmail")
        sent: list[dict] = []
        self._store(service, "acct-gmail", "INBOX", _inbox())
        service._get_correspondents_unlocked("acct-gmail")
        for i in range(120):
            sent.insert(
                0,
                {
                    "uid": f"s{i}",
                    "from": "Me <me@gmail.example>",
                    "to": "Bob <bob@work.example>",
                    "sort_date": NOW - 1000 + i,
                },
            )
            self._store(service, "acct-gmail", "[Gmail]/Sent Mail", sent)
            # Re-store the inbox as well, as a refresh would.
            self._store(service, "acct-gmail", "INBOX", _inbox())
            if i % 30 == 29:
                service = self._service("acct-gmail")
                service._get_correspondents_unlocked("acct-gmail")
        bob = self._by_email(service, "acct-gmail")["bob@work.example"]
        self.assertEqual(bob.sent_count, 120)
        self.assertEqual(bob.last_sent, NOW - 1000 + 119)

    def test_realistic_ranking_after_soak(self) -> None:
        service = self._service("acct-gmail")
        self._store(service, "acct-gmail", "INBOX", _inbox())
        service._get_correspondents_unlocked("acct-gmail")
        for _ in range(20):
            self._store(service, "acct-gmail", "INBOX", _inbox())
            self._store(service, "acct-gmail", "[Gmail]/Sent Mail", _sent())
            self._store(service, "acct-gmail", "Archive", _archive())

        # Nina is written to often; the newsletter wrote more recently and more
        # often but is received-only.
        self.assertEqual(self._order(service, "acct-gmail", "n")[:2], [
            "nina@work.example",
            "news@shop.example",
        ])
        # Own address stays suggestible and scores below real contacts.
        self.assertEqual(self._order(service, "acct-gmail", "me@"), ["me@gmail.example"])
        by_email = self._by_email(service, "acct-gmail")
        self.assertGreater(
            correspondent_score(by_email["nina@work.example"], NOW),
            correspondent_score(by_email["me@gmail.example"], NOW),
        )
        # Old, long-quiet contacts remain reachable.
        self.assertTrue(self._order(service, "acct-gmail", "friend"))

    def test_v1_cache_is_ignored_and_replaced_by_rebuilt_index(self) -> None:
        legacy = [
            {
                "display": f"Legacy {i} <legacy{i}@old.example>",
                "email": f"legacy{i}@old.example",
                "name": f"Legacy {i}",
                "last_seen": 1000 + i,
            }
            for i in range(600)
        ]
        path = correspondent_cache._cache_path("acct-gmail")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "correspondents": legacy}), encoding="utf-8"
        )

        service = self._service("acct-gmail")
        self.assertEqual(service._get_correspondents_unlocked("acct-gmail"), [])
        for _ in range(3):
            self._store(service, "acct-gmail", "INBOX", _inbox())
            self._store(service, "acct-gmail", "[Gmail]/Sent Mail", _sent())
        by_email = self._by_email(service, "acct-gmail")
        self.assertFalse(any(email.endswith("@old.example") for email in by_email))
        self.assertEqual(by_email["nina@work.example"].sent_count, 25)
        self.assertEqual(by_email["nina@work.example"].received_count, 60)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(
            set(saved["folders"]), {"INBOX", "[Gmail]/Sent Mail"}
        )

    def test_large_folder_restores_stay_cheap(self) -> None:
        big = [
            {
                "uid": str(i),
                "from": f"User {i % 400} <user{i % 400}@bulk.example>",
                "to": "Me <me@gmail.example>",
                "sort_date": NOW - i,
            }
            for i in range(20_000)
        ]
        service = self._service("acct-gmail")
        self._store(service, "acct-gmail", "INBOX", big)
        service._get_correspondents_unlocked("acct-gmail")
        started = time.monotonic()
        for _ in range(5):
            self._store(service, "acct-gmail", "INBOX", big)
        elapsed = time.monotonic() - started
        by_email = self._by_email(service, "acct-gmail")
        self.assertEqual(by_email["user7@bulk.example"].received_count, 50)
        # Generous bound; a regression to per-message object churn or repeated
        # full re-harvests would blow well past it.
        self.assertLess(elapsed, 30.0)


if __name__ == "__main__":
    unittest.main()
