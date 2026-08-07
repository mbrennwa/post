# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from post import __version__, _version_from_pyproject, get_version


def _pyproject_version() -> str:
    import tomllib

    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


class VersionResolutionTests(unittest.TestCase):
    def test_version_from_pyproject_matches_checkout(self) -> None:
        from_pyproject = _version_from_pyproject()
        self.assertIsNotNone(from_pyproject)
        self.assertEqual(from_pyproject, _pyproject_version())

    def test_get_version_prefers_pyproject_over_stale_metadata(self) -> None:
        expected = _version_from_pyproject()
        self.assertIsNotNone(expected)
        with mock.patch(
            "importlib.metadata.version",
            return_value="1.0.0.dev1",
        ) as metadata_version:
            self.assertEqual(get_version(), expected)
            metadata_version.assert_not_called()

    def test_get_version_falls_back_to_metadata_without_pyproject(self) -> None:
        with (
            mock.patch("post._version_from_pyproject", return_value=None),
            mock.patch(
                "importlib.metadata.version",
                return_value="9.9.9",
            ),
        ):
            self.assertEqual(get_version(), "9.9.9")

    def test_get_version_falls_back_to_hardcoded(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with (
            mock.patch("post._version_from_pyproject", return_value=None),
            mock.patch(
                "importlib.metadata.version",
                side_effect=PackageNotFoundError("post"),
            ),
        ):
            self.assertEqual(get_version(), __version__)


if __name__ == "__main__":
    unittest.main()
