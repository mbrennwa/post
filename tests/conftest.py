# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pytest defaults for Post tests."""

from __future__ import annotations

import os

# Unit tests use in-process Camel mocks via POST_MAIL_TEST_NO_CAMEL_HELPERS;
# do not spawn real helper processes.
os.environ.setdefault("POST_MAIL_TEST_NO_CAMEL_HELPERS", "1")
