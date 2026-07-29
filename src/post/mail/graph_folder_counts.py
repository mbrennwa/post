# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Microsoft Graph mailFolder counts for heavy-folder STATUS (#208).

Camel FolderInfo / open-folder summary for M365 Archive often reports only the
local summary size (hundreds–low thousands). Graph ``totalItemCount`` is the
real server total (tens of thousands) and is what Evolution’s STATUS-style
poll used to show as ``Archive (3803/28177)``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gio, GLib, Soup

log = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def graph_well_known_folder_id(folder_name: str) -> str | None:
    """Map a Post folder path to a Graph well-known mailFolder id, if any."""
    leaf = folder_name.rsplit("/", 1)[-1].strip().lower()
    if leaf in {"archive", "archives"}:
        return "archive"
    if leaf in {"junk", "junk email", "junkemail", "spam"}:
        return "junkemail"
    if leaf in {"trash", "deleted items", "deleteditems", "bin"}:
        return "deleteditems"
    if leaf in {"inbox"}:
        return "inbox"
    if leaf in {"sent", "sent items", "sentitems"}:
        return "sentitems"
    if leaf in {"drafts"}:
        return "drafts"
    return None


def fetch_mail_folder_counts(
    access_token: str,
    folder_name: str,
    *,
    cancellable: Gio.Cancellable | None = None,
) -> tuple[int, int] | None:
    """Return ``(unread, total)`` from Graph, or ``None`` on failure.

    Prefers well-known folder ids (``archive``, ``junkemail``, …). Falls back to
    listing top-level mailFolders and matching ``displayName``.
    """
    if not access_token:
        return None
    well_known = graph_well_known_folder_id(folder_name)
    if well_known is not None:
        counts = _get_folder_counts_url(
            access_token,
            f"{_GRAPH_BASE}/me/mailFolders/{quote(well_known, safe='')}"
            f"?$select=displayName,totalItemCount,unreadItemCount",
            cancellable=cancellable,
        )
        if counts is not None:
            return counts
    leaf = folder_name.rsplit("/", 1)[-1].strip()
    return _find_folder_counts_by_display_name(
        access_token, leaf, cancellable=cancellable
    )


def _find_folder_counts_by_display_name(
    access_token: str,
    display_name: str,
    *,
    cancellable: Gio.Cancellable | None,
) -> tuple[int, int] | None:
    url = (
        f"{_GRAPH_BASE}/me/mailFolders"
        f"?$select=displayName,totalItemCount,unreadItemCount"
        f"&$top=100"
    )
    while url:
        if cancellable is not None and cancellable.is_cancelled():
            return None
        payload = _send_json(access_token, url, cancellable=cancellable)
        if payload is None:
            return None
        for item in payload.get("value") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("displayName")
            if isinstance(name, str) and name.casefold() == display_name.casefold():
                return _counts_from_graph_folder(item)
        next_url = payload.get("@odata.nextLink")
        url = next_url if isinstance(next_url, str) and next_url else None
    return None


def _get_folder_counts_url(
    access_token: str,
    url: str,
    *,
    cancellable: Gio.Cancellable | None,
) -> tuple[int, int] | None:
    payload = _send_json(access_token, url, cancellable=cancellable)
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        log.debug("Graph mailFolder error: %s", payload.get("error"))
        return None
    return _counts_from_graph_folder(payload)


def _counts_from_graph_folder(item: dict[str, Any]) -> tuple[int, int] | None:
    total = item.get("totalItemCount")
    unread = item.get("unreadItemCount")
    if not isinstance(total, int) or total < 0:
        return None
    if not isinstance(unread, int) or unread < 0:
        unread = -1
    return unread, total


def _send_json(
    access_token: str,
    url: str,
    *,
    cancellable: Gio.Cancellable | None,
) -> dict[str, Any] | None:
    try:
        session = Soup.Session()
        message = Soup.Message.new("GET", url)
        if message is None:
            return None
        headers = message.get_request_headers()
        headers.append("Authorization", f"Bearer {access_token}")
        headers.append("Accept", "application/json")
        # Bound Graph STATUS lookups; do not pin mail I/O on a hung HTTP call.
        session.set_timeout(30)
        glist = session.send_and_read(message, cancellable)
        status = message.get_status()
        if status != Soup.Status.OK:
            log.debug("Graph HTTP %s for %s", int(status), url)
            return None
        raw = glist.get_data()
        if not raw:
            return None
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (GLib.Error, OSError, TypeError, ValueError, json.JSONDecodeError):
        log.debug("Graph request failed for %s", url, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    return payload
