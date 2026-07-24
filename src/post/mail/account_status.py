# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Runtime account online / connect health for sidebar badges (#168)."""

from __future__ import annotations

from typing import Literal

AccountConnectHealth = Literal["ok", "needs_sign_in", "not_connected"]

TOOLTIP_ACCOUNT_OFFLINE = "Account Offline"
TOOLTIP_NEEDS_SIGN_IN = "Needs sign-in"
TOOLTIP_NOT_CONNECTED = "Not connected"
TOOLTIP_NETWORK_OFFLINE = "Offline"


def account_not_online_badge(
    *,
    user_online: bool,
    connect_health: AccountConnectHealth,
    network_available: bool,
    remote_account: bool,
) -> tuple[bool, str]:
    """Return ``(show_badge, tooltip)`` for an account header marker.

    Intentional Take Offline (``user_online=False``) is separate from runtime
    connect/auth health and takes precedence for the tooltip.
    """
    if not user_online:
        return True, TOOLTIP_ACCOUNT_OFFLINE
    if connect_health == "needs_sign_in":
        return True, TOOLTIP_NEEDS_SIGN_IN
    if connect_health == "not_connected":
        return True, TOOLTIP_NOT_CONNECTED
    if remote_account and not network_available:
        return True, TOOLTIP_NETWORK_OFFLINE
    return False, ""
