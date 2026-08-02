# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Post — a simple GNOME mail client."""

__version__ = "1.0.0.dev1"

_PROJECT_HOMEPAGE = "https://mbrennwa.github.io/post"
_ISSUE_TRACKER_URL = "https://github.com/mbrennwa/post/issues"
_DEFAULT_DESCRIPTION = (
    "A simple GNOME mail client — GTK4 UI on Evolution Data Server"
)


def get_version() -> str:
    """Return the installed package version."""
    from importlib.metadata import PackageNotFoundError, version

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
