# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest

from post.mail.evolution_cache_path import (
    alternate_evolution_cache_path,
    cached_rfc822_candidates,
)


class EvolutionCachePathTests(unittest.TestCase):
    def test_rewrites_mail_subdir_to_store_root(self) -> None:
        mail = (
            "/home/u/.cache/evolution/mail/acct/folders/Inbox/cur/0f/deadbeef"
        )
        real = "/home/u/.cache/evolution/acct/folders/Inbox/cur/0f/deadbeef"
        self.assertEqual(alternate_evolution_cache_path(mail), real)
        self.assertEqual(cached_rfc822_candidates(mail), (mail, real))

    def test_leaves_non_mail_path_unchanged(self) -> None:
        path = "/home/u/.cache/evolution/acct/folders/Inbox/cur/0f/deadbeef"
        self.assertIsNone(alternate_evolution_cache_path(path))
        self.assertEqual(cached_rfc822_candidates(path), (path,))

    def test_ignores_unrelated_paths(self) -> None:
        path = "/tmp/not-evolution/mail/acct/message"
        self.assertIsNone(alternate_evolution_cache_path(path))
        self.assertEqual(cached_rfc822_candidates(path), (path,))

    def test_skips_empty_file_and_finds_nonempty_md5(self) -> None:
        import hashlib
        import tempfile
        from pathlib import Path

        from post.mail.evolution_cache_path import (
            find_nonempty_rfc822,
            first_nonempty_path,
            rfc822_digest,
        )

        uid = "AAMkExampleUid"
        digest = rfc822_digest(uid)
        self.assertEqual(digest, hashlib.md5(uid.encode("utf-8")).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            cur = Path(tmp) / "folders" / "Inbox" / "cur" / "0b"
            cur.mkdir(parents=True)
            empty = cur / digest
            empty.write_bytes(b"")
            self.assertIsNone(first_nonempty_path((str(empty),)))
            self.assertIsNone(find_nonempty_rfc822(tmp, ["Inbox"], digest))
            empty.write_bytes(b"From: a@example.com\r\n\r\nHi\r\n")
            found = find_nonempty_rfc822(tmp, ["Inbox"], digest)
            self.assertEqual(found, str(empty))


if __name__ == "__main__":
    unittest.main()
