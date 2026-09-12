# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reader context-menu helpers for inline pictures."""

from __future__ import annotations

import base64
from urllib.parse import unquote_to_bytes

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("WebKit", "6.0")

from gi.repository import GdkPixbuf, Gio, GLib, WebKit

LABEL_OPEN_PICTURE = "Open Picture"
LABEL_SAVE_PICTURE = "Save Picture"
LABEL_COPY_PICTURE = "Copy Picture"
LABEL_COPY_ADDRESS = "Copy Address"
LABEL_OPEN_LINK = "Open Link"
LABEL_COPY_LINK_ADDRESS = "Copy Link Address"

_IMAGE_STOCK_ACTIONS = frozenset(
    {
        WebKit.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW,
        WebKit.ContextMenuAction.DOWNLOAD_IMAGE_TO_DISK,
        WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD,
        WebKit.ContextMenuAction.COPY_IMAGE_URL_TO_CLIPBOARD,
        # Text copy is leftover browser chrome when the click is on an image.
        WebKit.ContextMenuAction.COPY,
        WebKit.ContextMenuAction.SELECT_ALL,
    }
)

_LINK_STOCK_ACTIONS = frozenset(
    {
        WebKit.ContextMenuAction.OPEN_LINK,
        WebKit.ContextMenuAction.OPEN_LINK_IN_NEW_WINDOW,
        WebKit.ContextMenuAction.DOWNLOAD_LINK_TO_DISK,
        WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD,
    }
)

_MIME_FILENAMES = {
    "image/png": "image.png",
    "image/jpeg": "image.jpeg",
    "image/jpg": "image.jpeg",
    "image/gif": "image.gif",
    "image/webp": "image.webp",
    "image/svg+xml": "image.svg",
    "image/bmp": "image.bmp",
    "image/x-icon": "image.ico",
    "image/vnd.microsoft.icon": "image.ico",
}


def is_embedded_image_uri(uri: str) -> bool:
    """Return True if *uri* is a ``data:`` image (CID rewritten in the reader)."""
    return isinstance(uri, str) and uri.lower().startswith("data:")


def is_remote_image_uri(uri: str) -> bool:
    """Return True if *uri* is an ``http(s)`` picture URL."""
    if not isinstance(uri, str):
        return False
    lower = uri.lower()
    return lower.startswith("http://") or lower.startswith("https://")


def is_http_link_uri(uri: str) -> bool:
    """Return True if *uri* should use Open Link / Copy Link Address."""
    return is_remote_image_uri(uri)


def decode_data_url(uri: str) -> tuple[str, bytes] | None:
    """Decode a ``data:`` URL into ``(mime_type, bytes)``, or ``None``."""
    if not is_embedded_image_uri(uri):
        return None
    header, separator, payload = uri.partition(",")
    if not separator or not payload:
        return None
    meta = header[5:]
    mime = "application/octet-stream"
    is_base64 = False
    if meta:
        parts = [part.strip() for part in meta.split(";") if part.strip()]
        if parts and "=" not in parts[0]:
            mime = parts[0]
            parts = parts[1:]
        is_base64 = any(part.lower() == "base64" for part in parts)
    try:
        if is_base64:
            data = base64.b64decode(payload, validate=False)
        else:
            data = unquote_to_bytes(payload)
    except (ValueError, TypeError):
        return None
    if not data:
        return None
    return mime, data


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def clipboard_png_bytes(data: bytes) -> bytes | None:
    """Return PNG bytes suitable for the clipboard, or ``None`` if undecodable.

    Paste targets on GTK4/Wayland expect ``image/png``, not a Gdk.Texture
    GValue. Already-PNG data is passed through; other formats are re-encoded.
    """
    if data.startswith(_PNG_MAGIC):
        return data
    try:
        loader = GdkPixbuf.PixbufLoader.new()
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except GLib.Error:
        return None
    if pixbuf is None:
        return None
    try:
        ok, buf = pixbuf.save_to_bufferv("png", [], [])
    except (GLib.Error, TypeError, ValueError):
        return None
    if not ok or not buf:
        return None
    return bytes(buf)


def guess_image_filename(mime_type: str | None) -> str:
    """Return a generic Save-as name from *mime_type*."""
    if not mime_type:
        return "image.png"
    key = mime_type.split(";", 1)[0].strip().lower()
    if key in _MIME_FILENAMES:
        return _MIME_FILENAMES[key]
    if key.startswith("image/") and "/" in key:
        subtype = key.split("/", 1)[1]
        if subtype and all(ch.isalnum() or ch in "-+" for ch in subtype):
            return f"image.{subtype}"
    return "image.png"


def strip_reader_image_stock_actions(
    menu: WebKit.ContextMenu, *, strip_link: bool = True
) -> None:
    """Remove WebKit stock image (and optional link) actions."""
    block = set(_IMAGE_STOCK_ACTIONS)
    if strip_link:
        block |= _LINK_STOCK_ACTIONS
    for item in list(menu.get_items()):
        if item.is_separator():
            continue
        if item.get_stock_action() in block:
            menu.remove(item)


def prepend_image_context_menu_items(
    menu: WebKit.ContextMenu,
    *,
    embedded: bool,
    http_link: bool,
    open_picture_action: Gio.Action,
    save_picture_action: Gio.Action,
    copy_picture_action: Gio.Action,
    copy_address_action: Gio.Action,
    open_link_action: Gio.Action,
    copy_link_action: Gio.Action,
) -> None:
    """Prepend Open / Save / Copy (or Copy Address) and optional link items.

    Visual order (top to bottom): picture actions, then Open Link / Copy Link
    Address when *http_link* is set. Call after mailto items so pictures stay
    at the top.
    """
    had_items = bool(list(menu.get_items()))
    if http_link:
        if had_items:
            menu.prepend(WebKit.ContextMenuItem.new_separator())
        menu.prepend(
            WebKit.ContextMenuItem.new_from_gaction(
                copy_link_action, LABEL_COPY_LINK_ADDRESS
            )
        )
        menu.prepend(
            WebKit.ContextMenuItem.new_from_gaction(open_link_action, LABEL_OPEN_LINK)
        )
        had_items = True
    if had_items:
        menu.prepend(WebKit.ContextMenuItem.new_separator())
    if embedded:
        menu.prepend(
            WebKit.ContextMenuItem.new_from_gaction(
                copy_picture_action, LABEL_COPY_PICTURE
            )
        )
    else:
        menu.prepend(
            WebKit.ContextMenuItem.new_from_gaction(
                copy_address_action, LABEL_COPY_ADDRESS
            )
        )
    menu.prepend(
        WebKit.ContextMenuItem.new_from_gaction(
            save_picture_action, LABEL_SAVE_PICTURE
        )
    )
    menu.prepend(
        WebKit.ContextMenuItem.new_from_gaction(
            open_picture_action, LABEL_OPEN_PICTURE
        )
    )
