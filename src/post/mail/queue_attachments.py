# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared attachment sidecar files for offline draft/send queues."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from .compose import ComposeAttachment


def write_attachment_sidecars(
    base_dir: str,
    queue_id: str,
    attachments: Sequence[ComposeAttachment],
) -> list[dict[str, str]]:
    """Write attachment bytes under *base_dir*/*queue_id*; return JSON refs."""
    if not attachments:
        return []
    directory = os.path.join(base_dir, queue_id)
    os.makedirs(directory, exist_ok=True)
    refs: list[dict[str, str]] = []
    for index, attachment in enumerate(attachments):
        rel_path = str(index)
        path = os.path.join(directory, rel_path)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".post-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(attachment.data)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        refs.append(
            {
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "path": rel_path,
            }
        )
    return refs


def load_attachment_sidecars(
    base_dir: str,
    queue_id: str,
    refs: Sequence[Mapping[str, Any]] | None,
) -> list[ComposeAttachment]:
    """Load attachments previously written by :func:`write_attachment_sidecars`."""
    if not refs:
        return []
    directory = os.path.join(base_dir, queue_id)
    loaded: list[ComposeAttachment] = []
    for ref in refs:
        rel_path = ref.get("path")
        if not rel_path:
            continue
        path = os.path.join(directory, str(rel_path))
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        loaded.append(
            ComposeAttachment(
                filename=str(ref.get("filename") or "attachment"),
                mime_type=str(ref.get("mime_type") or "application/octet-stream"),
                data=data,
            )
        )
    return loaded
