# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""WebKit-based plain-looking HTML compose body editor (Phase A of #206 / #24)."""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")

from gi.repository import Gtk, JavaScriptCore, WebKit

log = logging.getLogger(__name__)

_HANDLER_NAME = "composeChanged"

_EDITOR_DOCUMENT = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    background: transparent;
  }
  #editor {
    box-sizing: border-box;
    min-height: 100%;
    padding: 10px;
    outline: none;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    word-wrap: break-word;
    font-family: system-ui, sans-serif;
    font-size: 11pt;
    line-height: 1.4;
  }
</style>
</head>
<body>
<div id="editor" contenteditable="true" spellcheck="true">__COMPOSE_BODY__</div>
<script>
(function () {
  const editor = document.getElementById("editor");
  function notify() {
    try {
      window.webkit.messageHandlers.__COMPOSE_HANDLER__.postMessage({
        plain: editor.innerText,
        html: editor.innerHTML
      });
    } catch (e) {}
  }
  editor.addEventListener("input", notify);
  editor.addEventListener("keyup", notify);
  // Let Gtk DropTarget attach files; do not insert file:// URLs into the body.
  editor.addEventListener("dragover", function (e) { e.preventDefault(); });
  editor.addEventListener("drop", function (e) { e.preventDefault(); });
  window.__postCompose = {
    getPlain: function () { return editor.innerText; },
    getHtml: function () { return editor.innerHTML; },
    setPlain: function (text) {
      editor.innerText = text;
      notify();
    },
    setHtml: function (fragment) {
      editor.innerHTML = fragment;
      notify();
    },
    placeCursorAtStart: function () {
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(true);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      window.scrollTo(0, 0);
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    },
    placeCursorAtEnd: function () {
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    },
    scrollToTop: function () {
      window.scrollTo(0, 0);
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    }
  };
})();
</script>
</body>
</html>
"""


def build_editor_document(*, body_plain: str = "", body_html: str | None = None) -> str:
    """Build the trusted contenteditable shell document."""
    if body_html is not None:
        # Fragment only — never full documents from callers.
        body = body_html
    else:
        body = html.escape(body_plain)
    return (
        _EDITOR_DOCUMENT.replace("__COMPOSE_BODY__", body).replace(
            "__COMPOSE_HANDLER__", _HANDLER_NAME
        )
    )


def editor_html_is_plain_equivalent(body_html: str, body_plain: str) -> bool:
    """True when editor HTML is a trivial encoding of plain text (Phase A MIME)."""
    from post.mail.compose import is_plain_wrapper_html, plain_to_simple_html

    html_fragment = (body_html or "").strip()
    plain = body_plain or ""
    if not html_fragment:
        return not plain.strip()
    if is_plain_wrapper_html(html_fragment, plain):
        return True
    # contenteditable may use text nodes, <br>, or wrapping <div>s.
    normalized = html_fragment
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    # Exact escape of plain as text-only body.
    if normalized == html.escape(plain):
        return True
    if normalized == html.escape(plain.rstrip("\n")):
        return True
    # Single plain_to_simple_html wrapper already covered; also accept
    # escaped text with <br> for newlines.
    br_version = html.escape(plain.rstrip("\n")).replace("\n", "<br>")
    if normalized == br_version or normalized == br_version + "<br>":
        return True
    if plain_to_simple_html(plain) == html_fragment:
        return True
    return False


class ComposeBodyEditor(Gtk.Box):
    """Editable WebKit compose body with a plain-looking contenteditable UI."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(1, -1)

        self._cached_plain = ""
        self._cached_html = ""
        self._loaded = False
        self._pending_cursor_start = False
        self._pending_scroll_top = False
        self._cursor_offset = 0
        self._changed_handlers: list[Callable[[], None]] = []
        self._suppress_changed = True

        self._web_view = WebKit.WebView.new()
        settings = self._web_view.get_settings()
        settings.set_enable_javascript(True)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        settings.set_enable_tabs_to_links(False)
        self._web_view.set_hexpand(True)
        self._web_view.set_vexpand(True)
        self._web_view.set_halign(Gtk.Align.FILL)
        self._web_view.set_valign(Gtk.Align.FILL)
        self._web_view.set_size_request(1, -1)

        manager = self._web_view.get_user_content_manager()
        manager.register_script_message_handler(_HANDLER_NAME, None)
        manager.connect(
            f"script-message-received::{_HANDLER_NAME}",
            self._on_script_message,
        )

        self._web_view.connect("decide-policy", self._on_decide_policy)
        self._web_view.connect("load-changed", self._on_load_changed)
        self._web_view.connect("context-menu", self._on_context_menu)

        self.append(self._web_view)
        self._load_document(body_plain="")

    @property
    def web_view(self) -> WebKit.WebView:
        return self._web_view

    def connect_changed(self, callback: Callable[[], None]) -> None:
        self._changed_handlers.append(callback)

    def get_plain(self) -> str:
        return self._cached_plain

    def get_html(self) -> str:
        return self._cached_html

    def set_plain(self, text: str) -> None:
        """Replace the entire body from plain text (reloads the editor document)."""
        self._suppress_changed = True
        self._cached_plain = text
        self._cached_html = html.escape(text)
        self._load_document(body_plain=text)

    def set_html_fragment(self, fragment: str) -> None:
        """Replace editor contents with an HTML fragment (not a full document)."""
        self._suppress_changed = True
        self._cached_html = fragment
        self._load_document(body_html=fragment)

    def grab_focus(self) -> bool:  # type: ignore[override]
        return self._web_view.grab_focus()

    def place_cursor_at_start(self) -> None:
        self._cursor_offset = 0
        if not self._loaded:
            self._pending_cursor_start = True
            return
        self._run_js("window.__postCompose.placeCursorAtStart();")

    def place_cursor_at_end(self) -> None:
        self._cursor_offset = max(0, len(self._cached_plain))
        if self._loaded:
            self._run_js("window.__postCompose.placeCursorAtEnd();")

    def cursor_offset(self) -> int:
        """Best-effort caret offset for tests (0 after place_cursor_at_start)."""
        return self._cursor_offset

    def scroll_to_top(self) -> None:
        if not self._loaded:
            self._pending_scroll_top = True
            return
        self._run_js("window.__postCompose.scrollToTop();")

    def _load_document(
        self, *, body_plain: str = "", body_html: str | None = None
    ) -> None:
        self._loaded = False
        document = build_editor_document(body_plain=body_plain, body_html=body_html)
        self._web_view.load_html(document, None)

    def _run_js(self, script: str) -> None:
        self._web_view.evaluate_javascript(
            script,
            -1,
            None,
            None,
            None,
            None,
            None,
        )

    def _on_load_changed(
        self, _web_view: WebKit.WebView, event: WebKit.LoadEvent
    ) -> None:
        if event != WebKit.LoadEvent.FINISHED:
            return
        self._loaded = True
        if self._pending_cursor_start:
            self._pending_cursor_start = False
            self.place_cursor_at_start()
        if self._pending_scroll_top:
            self._pending_scroll_top = False
            self.scroll_to_top()
        self._suppress_changed = False

    def _on_script_message(
        self,
        _manager: WebKit.UserContentManager,
        value: JavaScriptCore.Value,
    ) -> None:
        try:
            if value.is_object():
                plain_v = value.object_get_property("plain")
                html_v = value.object_get_property("html")
                if plain_v is not None and plain_v.is_string():
                    self._cached_plain = plain_v.to_string()
                if html_v is not None and html_v.is_string():
                    self._cached_html = html_v.to_string()
            elif value.is_string():
                # Fallback if a bare string is posted.
                self._cached_plain = value.to_string()
        except Exception:
            log.debug("compose editor script message parse failed", exc_info=True)
            return
        if self._suppress_changed:
            return
        for handler in list(self._changed_handlers):
            try:
                handler()
            except Exception:
                log.exception("compose body changed handler failed")

    def _on_decide_policy(
        self,
        _web_view: WebKit.WebView,
        decision: WebKit.NavigationPolicyDecision,
        decision_type: WebKit.PolicyDecisionType,
    ) -> bool:
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False
        try:
            action = decision.get_navigation_action()
            nav_type = action.get_navigation_type()
        except Exception:
            decision.ignore()
            return True
        # Allow the initial document load; block anything else.
        if nav_type == WebKit.NavigationType.OTHER:
            return False
        decision.ignore()
        return True

    def _on_context_menu(
        self,
        _web_view: WebKit.WebView,
        menu: WebKit.ContextMenu,
        _hit_test_result: WebKit.HitTestResult,
    ) -> bool:
        # Strip navigation chrome; keep editing / spell-check items for later phases.
        remove_actions = {
            int(WebKit.ContextMenuAction.GO_BACK),
            int(WebKit.ContextMenuAction.GO_FORWARD),
            int(WebKit.ContextMenuAction.STOP),
            int(WebKit.ContextMenuAction.RELOAD),
            int(WebKit.ContextMenuAction.OPEN_LINK),
            int(WebKit.ContextMenuAction.OPEN_LINK_IN_NEW_WINDOW),
            int(WebKit.ContextMenuAction.DOWNLOAD_LINK_TO_DISK),
        }
        for item in list(menu.get_items()):
            try:
                action = int(item.get_stock_action())
            except Exception:
                continue
            if action in remove_actions:
                menu.remove(item)
        return False
