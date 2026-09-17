# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pytest defaults for Post tests."""

from __future__ import annotations

import os

# Unit tests use in-process Camel mocks; opt into helpers in dedicated tests (#437).
os.environ.setdefault("POST_MAIL_CAMEL_HELPERS", "0")
