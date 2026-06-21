# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mail backend: Evolution Data Server + Camel."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .eds import MailService

__all__ = ["MailService"]


def __getattr__(name: str) -> object:
    if name == "MailService":
        from .eds import MailService

        return MailService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
