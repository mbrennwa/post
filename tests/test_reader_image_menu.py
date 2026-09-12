# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import unittest

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")

from gi.repository import GdkPixbuf, Gio, Gtk, WebKit

from post.reader.image_menu import (
    LABEL_COPY_ADDRESS,
    LABEL_COPY_LINK_ADDRESS,
    LABEL_COPY_PICTURE,
    LABEL_OPEN_LINK,
    LABEL_OPEN_PICTURE,
    LABEL_SAVE_PICTURE,
    clipboard_png_bytes,
    decode_data_url,
    guess_image_filename,
    is_embedded_image_uri,
    is_http_link_uri,
    is_remote_image_uri,
    prepend_image_context_menu_items,
    strip_reader_image_stock_actions,
)

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _menu_actions(menu: WebKit.ContextMenu) -> list[str | int]:
    actions: list[str | int] = []
    for item in menu.get_items():
        if item.is_separator():
            actions.append("SEP")
        else:
            stock = int(item.get_stock_action())
            if stock == int(WebKit.ContextMenuAction.CUSTOM):
                actions.append(item.get_title() or "CUSTOM")
            else:
                actions.append(stock)
    return actions


def _actions() -> dict[str, Gio.SimpleAction]:
    names = (
        "open_picture",
        "save_picture",
        "copy_picture",
        "copy_address",
        "open_link",
        "copy_link",
    )
    return {name: Gio.SimpleAction.new(name.replace("_", "-"), None) for name in names}


class ImageMenuHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Gtk.is_initialized():
            Gtk.init()

    def test_classify_uris(self) -> None:
        self.assertTrue(is_embedded_image_uri("data:image/png;base64,abcd"))
        self.assertTrue(is_remote_image_uri("https://cdn.example/logo.png"))
        self.assertTrue(is_http_link_uri("http://example.com/go"))
        self.assertFalse(is_embedded_image_uri("https://cdn.example/logo.png"))
        self.assertFalse(is_remote_image_uri("data:image/png;base64,abcd"))
        self.assertFalse(is_remote_image_uri("cid:logo@local"))
        self.assertFalse(is_http_link_uri("mailto:a@example.com"))

    def test_decode_data_url_base64_png(self) -> None:
        uri = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
        decoded = decode_data_url(uri)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        mime, data = decoded
        self.assertEqual(mime, "image/png")
        self.assertEqual(data, _PNG)

    def test_clipboard_png_bytes_passthrough(self) -> None:
        self.assertEqual(clipboard_png_bytes(_PNG), _PNG)

    def test_clipboard_png_bytes_rejects_garbage(self) -> None:
        self.assertIsNone(clipboard_png_bytes(b"not-an-image"))

    def test_clipboard_png_bytes_reencodes_jpeg(self) -> None:
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 1, 1)
        ok, jpeg = pixbuf.save_to_bufferv("jpeg", ["quality"], ["90"])
        self.assertTrue(ok)
        png = clipboard_png_bytes(bytes(jpeg))
        self.assertIsNotNone(png)
        assert png is not None
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_decode_data_url_rejects_garbage(self) -> None:
        self.assertIsNone(decode_data_url("https://example/x.png"))
        self.assertIsNone(decode_data_url("data:image/png;base64,"))
        self.assertIsNone(decode_data_url("data:"))

    def test_guess_image_filename(self) -> None:
        self.assertEqual(guess_image_filename("image/png"), "image.png")
        self.assertEqual(guess_image_filename("image/jpeg; charset=binary"), "image.jpeg")
        self.assertEqual(guess_image_filename("image/webp"), "image.webp")
        self.assertEqual(guess_image_filename(None), "image.png")
        self.assertEqual(guess_image_filename("application/octet-stream"), "image.png")

    def test_strip_stock_image_and_link_actions(self) -> None:
        menu = WebKit.ContextMenu.new()
        for action in (
            WebKit.ContextMenuAction.OPEN_IMAGE_IN_NEW_WINDOW,
            WebKit.ContextMenuAction.DOWNLOAD_IMAGE_TO_DISK,
            WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD,
            WebKit.ContextMenuAction.COPY_IMAGE_URL_TO_CLIPBOARD,
            WebKit.ContextMenuAction.OPEN_LINK,
            WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD,
            WebKit.ContextMenuAction.COPY,
            WebKit.ContextMenuAction.GO_BACK,
        ):
            menu.append(WebKit.ContextMenuItem.new_from_stock_action(action))
        strip_reader_image_stock_actions(menu, strip_link=True)
        self.assertEqual(
            _menu_actions(menu),
            [int(WebKit.ContextMenuAction.GO_BACK)],
        )

    def test_prepend_embedded_picture_items(self) -> None:
        menu = WebKit.ContextMenu.new()
        actions = _actions()
        prepend_image_context_menu_items(
            menu,
            embedded=True,
            http_link=False,
            open_picture_action=actions["open_picture"],
            save_picture_action=actions["save_picture"],
            copy_picture_action=actions["copy_picture"],
            copy_address_action=actions["copy_address"],
            open_link_action=actions["open_link"],
            copy_link_action=actions["copy_link"],
        )
        self.assertEqual(
            _menu_actions(menu),
            [LABEL_OPEN_PICTURE, LABEL_SAVE_PICTURE, LABEL_COPY_PICTURE],
        )

    def test_prepend_remote_picture_items(self) -> None:
        menu = WebKit.ContextMenu.new()
        actions = _actions()
        prepend_image_context_menu_items(
            menu,
            embedded=False,
            http_link=False,
            open_picture_action=actions["open_picture"],
            save_picture_action=actions["save_picture"],
            copy_picture_action=actions["copy_picture"],
            copy_address_action=actions["copy_address"],
            open_link_action=actions["open_link"],
            copy_link_action=actions["copy_link"],
        )
        self.assertEqual(
            _menu_actions(menu),
            [LABEL_OPEN_PICTURE, LABEL_SAVE_PICTURE, LABEL_COPY_ADDRESS],
        )
        self.assertNotIn(LABEL_COPY_PICTURE, _menu_actions(menu))

    def test_prepend_linked_embedded_picture_items(self) -> None:
        menu = WebKit.ContextMenu.new()
        actions = _actions()
        prepend_image_context_menu_items(
            menu,
            embedded=True,
            http_link=True,
            open_picture_action=actions["open_picture"],
            save_picture_action=actions["save_picture"],
            copy_picture_action=actions["copy_picture"],
            copy_address_action=actions["copy_address"],
            open_link_action=actions["open_link"],
            copy_link_action=actions["copy_link"],
        )
        self.assertEqual(
            _menu_actions(menu),
            [
                LABEL_OPEN_PICTURE,
                LABEL_SAVE_PICTURE,
                LABEL_COPY_PICTURE,
                "SEP",
                LABEL_OPEN_LINK,
                LABEL_COPY_LINK_ADDRESS,
            ],
        )

    def test_picture_items_sit_above_existing_mailto_items(self) -> None:
        menu = WebKit.ContextMenu.new()
        mailto = Gio.SimpleAction.new("address-copy", None)
        menu.prepend(
            WebKit.ContextMenuItem.new_from_gaction(mailto, "Copy address")
        )
        actions = _actions()
        prepend_image_context_menu_items(
            menu,
            embedded=True,
            http_link=False,
            open_picture_action=actions["open_picture"],
            save_picture_action=actions["save_picture"],
            copy_picture_action=actions["copy_picture"],
            copy_address_action=actions["copy_address"],
            open_link_action=actions["open_link"],
            copy_link_action=actions["copy_link"],
        )
        self.assertEqual(
            _menu_actions(menu),
            [
                LABEL_OPEN_PICTURE,
                LABEL_SAVE_PICTURE,
                LABEL_COPY_PICTURE,
                "SEP",
                "Copy address",
            ],
        )


if __name__ == "__main__":
    unittest.main()
