# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from post.mail.camel_paths import (
    ENV_CACHE,
    ENV_DATA,
    configure_helper_camel_dirs,
    default_evolution_dirs,
    helper_camel_dirs,
    resolve_camel_dirs,
)


class CamelPathsTests(unittest.TestCase):
    def test_helper_dirs_are_per_account_under_post_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}, clear=False):
                data, cache = helper_camel_dirs("acct/one")
            self.assertTrue(data.startswith(tmp))
            self.assertIn("camel-helper", data)
            self.assertTrue(data.endswith(os.path.join("acct_one", "data")))
            self.assertTrue(cache.endswith(os.path.join("acct_one", "cache")))
            self.assertNotEqual(data, default_evolution_dirs()[0])

    def test_configure_exports_env_and_creates_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}, clear=False):
                data, cache = configure_helper_camel_dirs("uid-42")
                self.assertTrue(Path(data).is_dir())
                self.assertTrue(Path(cache).is_dir())
                self.assertEqual(os.environ[ENV_DATA], data)
                self.assertEqual(os.environ[ENV_CACHE], cache)
                self.assertEqual(resolve_camel_dirs(), (data, cache))

    def test_resolve_falls_back_to_system_evolution(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_DATA, None)
            os.environ.pop(ENV_CACHE, None)
            self.assertEqual(resolve_camel_dirs(), default_evolution_dirs())


if __name__ == "__main__":
    unittest.main()
