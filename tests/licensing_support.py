# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Helpers for licensing compliance tests."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSING_DIR = REPO_ROOT / "licensing"

DERIVED_FILES_RE = re.compile(
    r"^Derived Post files:\s*(.+)$",
    re.MULTILINE,
)


def load_json(relative_path: str) -> dict:
    path = REPO_ROOT / relative_path
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def glob_paths(pattern: str) -> list[Path]:
    """Expand a repo-relative glob like ``src/**/*.py``."""
    if "**" not in pattern:
        return sorted(REPO_ROOT.glob(pattern))

    prefix, _, suffix = pattern.partition("/**/")
    base = REPO_ROOT / prefix
    if not base.is_dir():
        return []
    return sorted(
        path
        for path in base.rglob(suffix)
        if path.is_file() and fnmatch.fnmatch(str(path.relative_to(REPO_ROOT)), pattern)
    )


def read_pyproject_license_files() -> list[str]:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r'license-files\s*=\s*\[(.*?)\]',
        pyproject,
        re.DOTALL,
    )
    if match is None:
        return []
    entries = re.findall(r'"([^"]+)"', match.group(1))
    expanded: list[str] = []
    for entry in entries:
        if "*" in entry:
            expanded.extend(
                str(path.relative_to(REPO_ROOT))
                for path in sorted(REPO_ROOT.glob(entry))
            )
        else:
            expanded.append(entry)
    return expanded


def parse_derived_files(license_file: Path) -> list[str]:
    text = license_file.read_text(encoding="utf-8")
    match = DERIVED_FILES_RE.search(text)
    if match is None:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]
