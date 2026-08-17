# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Post — a simple GNOME mail client."""

from __future__ import annotations

from pathlib import Path

__version__ = "1.0.0a2"

_PROJECT_HOMEPAGE = "https://mbrennwa.github.io/post"
_ISSUE_TRACKER_URL = "https://github.com/mbrennwa/post/issues"
_DEFAULT_DESCRIPTION = (
    "A simple GNOME mail client — GTK4 UI on Evolution Data Server"
)


def _version_from_pyproject() -> str | None:
    """Read ``[project].version`` from the source-tree pyproject when present."""
    # src/post/__init__.py → repo root is parents[2]
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python < 3.11
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError:
        return None
    value = data.get("project", {}).get("version")
    return str(value) if value else None


def get_version() -> str:
    """Return the package version for About / bug reports.

    Prefer ``pyproject.toml`` in a source checkout so editable installs stay
    correct when dist-info is stale after a version bump. Fall back to
    installed metadata, then the hardcoded ``__version__``.
    """
    from importlib.metadata import PackageNotFoundError, version

    from_pyproject = _version_from_pyproject()
    if from_pyproject:
        return from_pyproject
    try:
        return version("post")
    except PackageNotFoundError:
        return __version__


def get_app_description() -> str:
    """Return the installed package summary."""
    from importlib.metadata import PackageNotFoundError, metadata

    try:
        return metadata("post")["Summary"]
    except PackageNotFoundError:
        return _DEFAULT_DESCRIPTION
