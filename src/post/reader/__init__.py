# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Message reading pane (HTML rendering)."""

from .html import build_reader_document
from .pane import MessageReaderPane

__all__ = ["MessageReaderPane", "build_reader_document"]
