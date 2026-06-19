# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mail backend: Evolution Data Server + Camel."""

from .eds import MailService

__all__ = ["MailService"]
