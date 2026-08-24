# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""WebKit-based HTML compose body editor (Phase B of #206 / #24)."""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")

from gi.repository import Gio, GLib, Gtk, JavaScriptCore, WebKit

from post.open_uri import open_uri_externally
from post.reader.pane import (
    apply_reader_link_hover,
    pointer_coords_in_widget,
    reader_link_hover_origin,
    reader_link_tooltip_text,
)
from post.spell_check import (
    action_name_for_language,
    get_active_spell_languages,
    list_installed_spell_languages,
    set_spell_language_active,
)

log = logging.getLogger(__name__)

_HANDLER_NAME = "composeChanged"
_ACTION_HANDLER = "composeAction"

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
  #editor blockquote {
    margin: 0.25em 0 0.25em 0.25em;
    padding-left: 0.75em;
    border-left: 3px solid #888;
  }
  #editor a { color: LinkText; text-decoration: underline; }
</style>
</head>
<body>
<div id="editor" contenteditable="true" spellcheck="true">__COMPOSE_BODY__</div>
<script>
(function () {
  const editor = document.getElementById("editor");
  let savedRange = null;
  function stampLinks() {
    editor.querySelectorAll("a").forEach(function (a) {
      const href = a.getAttribute("href") || a.href || "";
      if (href && href.indexOf("javascript:") !== 0) {
        a.setAttribute("href", href);
        a.setAttribute("data-post-href", href);
      }
    });
  }
  function notify() {
    try {
      stampLinks();
      window.webkit.messageHandlers.__COMPOSE_HANDLER__.postMessage({
        plain: editor.innerText,
        html: editor.innerHTML
      });
    } catch (e) {}
  }
  function saveSelection() {
    const sel = window.getSelection();
    if (sel && sel.rangeCount) savedRange = sel.getRangeAt(0).cloneRange();
    else savedRange = null;
  }
  function restoreSelection() {
    editor.focus();
    if (!savedRange) return;
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(savedRange);
  }
  function closestBlock(node) {
    let n = node;
    if (n && n.nodeType === Node.TEXT_NODE) n = n.parentNode;
    while (n && n !== editor) {
      const name = n.nodeName;
      if (name === "DIV" || name === "P" || name === "BLOCKQUOTE" || name === "LI") {
        return n;
      }
      n = n.parentNode;
    }
    return null;
  }
  function getLinkState() {
    const sel = window.getSelection();
    const text = sel ? sel.toString() : "";
    let href = "";
    let node = sel && sel.anchorNode;
    if (node && node.nodeType === Node.TEXT_NODE) node = node.parentNode;
    while (node && node !== editor) {
      if (node.nodeName === "A") {
        href = node.getAttribute("href") || "";
        break;
      }
      node = node.parentNode;
    }
    return JSON.stringify({ text: text, href: href });
  }
  function requestLink() {
    saveSelection();
    try {
      const state = JSON.parse(getLinkState());
      window.webkit.messageHandlers.__COMPOSE_ACTION__.postMessage({
        action: "link",
        text: state.text || "",
        href: state.href || ""
      });
    } catch (e) {}
  }
  function increaseQuote() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    let range = sel.getRangeAt(0);
    if (range.collapsed) {
      const block = closestBlock(range.startContainer);
      if (block && block !== editor) {
        range = document.createRange();
        range.selectNode(block);
      } else if (range.startContainer.nodeType === Node.TEXT_NODE) {
        range = document.createRange();
        range.selectNode(range.startContainer);
      }
    }
    const bq = document.createElement("blockquote");
    try {
      range.surroundContents(bq);
    } catch (e) {
      const contents = range.extractContents();
      bq.appendChild(contents);
      range.insertNode(bq);
    }
    notify();
  }
  function decreaseQuote() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    let node = sel.anchorNode;
    if (node && node.nodeType === Node.TEXT_NODE) node = node.parentNode;
    while (node && node !== editor) {
      if (node.nodeName === "BLOCKQUOTE") {
        const parent = node.parentNode;
        while (node.firstChild) parent.insertBefore(node.firstChild, node);
        parent.removeChild(node);
        break;
      }
      node = node.parentNode;
    }
    notify();
  }
  function exec(command) {
    document.execCommand(command, false, null);
    notify();
  }
  editor.addEventListener("input", notify);
  editor.addEventListener("keyup", notify);
  editor.addEventListener("mouseup", saveSelection);
  editor.addEventListener("keyup", saveSelection);
  editor.addEventListener("keydown", function (e) {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    if (e.code === "KeyB") { e.preventDefault(); exec("bold"); }
    else if (e.code === "KeyI") { e.preventDefault(); exec("italic"); }
    else if (e.code === "KeyX" && e.shiftKey) { e.preventDefault(); exec("strikeThrough"); }
    else if (e.code === "KeyK") { e.preventDefault(); requestLink(); }
    else if (e.code === "BracketRight") { e.preventDefault(); saveSelection(); increaseQuote(); }
    else if (e.code === "BracketLeft") { e.preventDefault(); saveSelection(); decreaseQuote(); }
    else if (e.code === "Backslash") { e.preventDefault(); exec("removeFormat"); }
  });
  editor.addEventListener("click", function (e) {
    let node = e.target;
    if (node && node.nodeType === Node.TEXT_NODE) node = node.parentNode;
    while (node && node !== editor) {
      if (node.nodeName === "A") {
        const href = node.getAttribute("data-post-href")
          || node.getAttribute("href")
          || node.href
          || "";
        if (href && href.indexOf("javascript:") !== 0) {
          e.preventDefault();
          e.stopPropagation();
          try {
            window.webkit.messageHandlers.__COMPOSE_ACTION__.postMessage({
              action: "open-link",
              href: href
            });
          } catch (err) {}
        }
        return;
      }
      node = node.parentNode;
    }
  }, true);
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
    exec: function (command) { restoreSelection(); exec(command); },
    increaseQuote: function () { restoreSelection(); increaseQuote(); },
    decreaseQuote: function () { restoreSelection(); decreaseQuote(); },
    removeFormat: function () { restoreSelection(); exec("removeFormat"); },
    beginLink: function () {
      saveSelection();
      return getLinkState();
    },
    createLink: function (url) {
      restoreSelection();
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return;
      let node = sel.anchorNode;
      if (node && node.nodeType === Node.TEXT_NODE) node = node.parentNode;
      while (node && node !== editor) {
        if (node.nodeName === "A") {
          node.setAttribute("href", url);
          try { node.href = url; } catch (e) {}
          notify();
          return;
        }
        node = node.parentNode;
      }
      const a = document.createElement("a");
      a.setAttribute("href", url);
      try { a.href = url; } catch (e) {}
      const range = sel.getRangeAt(0);
      if (range.collapsed) {
        a.textContent = url;
        range.insertNode(a);
      } else {
        try {
          range.surroundContents(a);
        } catch (e) {
          a.appendChild(range.extractContents());
          range.insertNode(a);
        }
      }
      notify();
    },
    removeLink: function () {
      restoreSelection();
      document.execCommand("unlink", false, null);
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
        _EDITOR_DOCUMENT.replace("__COMPOSE_BODY__", body)
        .replace("__COMPOSE_HANDLER__", _HANDLER_NAME)
        .replace("__COMPOSE_ACTION__", _ACTION_HANDLER)
    )


def editor_html_is_plain_equivalent(body_html: str, body_plain: str) -> bool:
    """True when editor HTML is a trivial encoding of plain text (Phase A MIME)."""
    from post.mail.compose import is_plain_wrapper_html, plain_to_simple_html

    html_fragment = body_html or ""
    plain = body_plain or ""
    if not html_fragment.strip():
        return not plain.strip()
    if is_plain_wrapper_html(html_fragment, plain):
        return True
    if plain_to_simple_html(plain) == html_fragment.strip():
        return True
    comparable = _plain_from_structural_html(html_fragment)
    if comparable is None:
        return False
    return _canonical_plain(comparable) == _canonical_plain(plain)


def _canonical_plain(text: str) -> str:
    """Normalize editor plain/HTML text for MIME-mode comparison."""
    return (
        (text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\xa0", " ")
        .rstrip()
    )


def _plain_from_structural_html(fragment: str) -> str | None:
    """Return plain text if *fragment* has only structural wrappers, else None."""
    text = fragment.replace("\r\n", "\n").replace("\r", "\n")
    # contenteditable stores trailing/consecutive spaces as &nbsp;.
    text = re.sub(r"(?i)&nbsp;|&#160;|&#xa0;", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</div>\s*<div>", "\n", text)
    text = re.sub(r"(?i)</p>\s*<p>", "\n\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)<div>", "", text)
    text = re.sub(r"(?i)<p>", "", text)
    # WebKit may wrap converted spaces in a classed <span>; strip those first.
    text = re.sub(r"(?i)<span\b[^>]*>\s*</span>", " ", text)
    text = re.sub(r"(?i)</?span>", "", text)
    if re.search(r"<[^>]+>", text):
        return None
    text = html.unescape(text).replace("\xa0", " ")
    return text


_SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")


def compose_uri_opens_externally(uri: str) -> bool:
    """True when a composer/reader link should open in the desktop handler."""
    return (uri or "").strip().lower().startswith(_SAFE_LINK_PREFIXES)


def normalize_compose_link_url(raw: str) -> str | None:
    """Return a safe http(s)/mailto URL, or None when the input is empty/unsafe."""
    url = (raw or "").strip()
    if not url:
        return None
    lower = url.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:")):
        return None
    if lower.startswith("mailto:"):
        return url
    if "://" in url:
        if not any(lower.startswith(prefix) for prefix in _SAFE_LINK_PREFIXES):
            return None
        return url
    if "@" in url and "/" not in url and " " not in url:
        return f"mailto:{url}"
    return f"https://{url}"


class ComposeBodyEditor(Gtk.Box):
    """Editable WebKit compose body with a formatting bar."""

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
        self._link_request_handlers: list[Callable[[str, str], None]] = []
        self._suppress_changed = True
        self._spell_action_group = Gio.SimpleActionGroup.new()
        self._spell_language_actions: dict[str, Gio.SimpleAction] = {}

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
        manager.register_script_message_handler(_ACTION_HANDLER, None)
        manager.connect(
            f"script-message-received::{_ACTION_HANDLER}",
            self._on_action_message,
        )

        self._web_view.connect("decide-policy", self._on_decide_policy)
        self._web_view.connect("load-changed", self._on_load_changed)
        self._web_view.connect("context-menu", self._on_context_menu)
        self._web_view.connect(
            "mouse-target-changed", self._on_mouse_target_changed
        )

        self._web_view.insert_action_group("compose-spell", self._spell_action_group)
        self._rebuild_spell_language_actions()
        self._unlink_action = Gio.SimpleAction.new("remove-link", None)
        self._unlink_action.connect("activate", lambda *_a: self.remove_link())
        format_actions = Gio.SimpleActionGroup.new()
        format_actions.add_action(self._unlink_action)
        self._web_view.insert_action_group("compose-format", format_actions)
        self._hover_link_uri: str | None = None
        self._opened_uri_time = 0

        self.append(self._build_format_bar())
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(self._web_view)
        self._link_hover_label = Gtk.Label(label="", xalign=0)
        self._link_hover_label.set_ellipsize(3)
        self._link_hover_label.set_max_width_chars(80)
        self._link_hover_label.set_single_line_mode(True)
        self._link_hover_label.set_can_target(False)
        self._link_hover_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self._link_hover_box.add_css_class("osd")
        self._link_hover_box.set_can_target(False)
        self._link_hover_box.set_halign(Gtk.Align.START)
        self._link_hover_box.set_valign(Gtk.Align.START)
        self._link_hover_box.set_visible(False)
        self._link_hover_box.append(self._link_hover_label)
        overlay.add_overlay(self._link_hover_box)
        self._link_hover_overlay = overlay
        self.append(overlay)
        self._load_document(body_plain="")

    @property
    def web_view(self) -> WebKit.WebView:
        return self._web_view

    def connect_changed(self, callback: Callable[[], None]) -> None:
        self._changed_handlers.append(callback)

    def connect_link_request(self, callback: Callable[[str, str], None]) -> None:
        self._link_request_handlers.append(callback)

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

    def exec_format(self, command: str) -> None:
        self._run_js(
            f"window.__postCompose.exec({json.dumps(command)});"
        )

    def increase_quote(self) -> None:
        self._run_js("window.__postCompose.increaseQuote();")

    def decrease_quote(self) -> None:
        self._run_js("window.__postCompose.decreaseQuote();")

    def remove_format(self) -> None:
        self._run_js("window.__postCompose.removeFormat();")

    def apply_link(self, url: str) -> None:
        self._run_js(f"window.__postCompose.createLink({json.dumps(url)});")

    def remove_link(self) -> None:
        self._run_js("window.__postCompose.removeLink();")

    def request_link_dialog(self) -> None:
        self._web_view.evaluate_javascript(
            "window.__postCompose.beginLink();",
            -1,
            None,
            None,
            None,
            self._on_begin_link_finished,
        )

    def _build_format_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.add_css_class("toolbar")

        style = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        style.add_css_class("linked")
        style.append(
            self._format_button(
                "format-text-bold-symbolic",
                "Bold (Ctrl+B)",
                lambda *_a: self.exec_format("bold"),
            )
        )
        style.append(
            self._format_button(
                "format-text-italic-symbolic",
                "Italic (Ctrl+I)",
                lambda *_a: self.exec_format("italic"),
            )
        )
        style.append(
            self._format_button(
                "format-text-strikethrough-symbolic",
                "Strikethrough (Ctrl+Shift+X)",
                lambda *_a: self.exec_format("strikeThrough"),
            )
        )
        bar.append(style)

        quote = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        quote.add_css_class("linked")
        quote.append(
            self._format_button(
                "format-indent-more-symbolic",
                "Increase quote level (Ctrl+])",
                lambda *_a: self.increase_quote(),
            )
        )
        quote.append(
            self._format_button(
                "format-indent-less-symbolic",
                "Decrease quote level (Ctrl+[)",
                lambda *_a: self.decrease_quote(),
            )
        )
        bar.append(quote)

        bar.append(
            self._format_button(
                "insert-link-symbolic",
                "Insert or edit link (Ctrl+K)",
                lambda *_a: self.request_link_dialog(),
            )
        )
        bar.append(
            self._format_button(
                "edit-clear-symbolic",
                "Remove formatting (Ctrl+\\)",
                lambda *_a: self.remove_format(),
            )
        )
        self._mode_label = Gtk.Label(label="Plain Text")
        self._mode_label.add_css_class("dim-label")
        self._mode_label.add_css_class("caption")
        self._mode_label.set_xalign(1.0)
        self._mode_label.set_hexpand(True)
        self._mode_label.set_can_focus(False)
        self._mode_label.set_tooltip_text(
            "Plain Text: send as text only. HTML: send a formatted alternative."
        )
        bar.append(self._mode_label)
        return bar

    def _format_button(
        self,
        icon_name: str,
        tooltip: str,
        on_clicked: Callable[..., None],
    ) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon_name)
        button.set_tooltip_text(tooltip)
        button.set_focus_on_click(False)
        button.set_can_focus(False)
        button.add_css_class("flat")
        button.connect("clicked", on_clicked)
        return button

    def _refresh_format_mode_label(self) -> None:
        label = getattr(self, "_mode_label", None)
        if label is None:
            return
        html_mode = not editor_html_is_plain_equivalent(
            self._cached_html, self._cached_plain
        )
        label.set_label("HTML" if html_mode else "Plain Text")
        label.add_css_class("dim-label")
        label.remove_css_class("accent")

    def _load_document(
        self, *, body_plain: str = "", body_html: str | None = None
    ) -> None:
        self._loaded = False
        document = build_editor_document(body_plain=body_plain, body_html=body_html)
        self._web_view.load_html(document, "about:blank")

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

    def _on_begin_link_finished(
        self,
        web_view: WebKit.WebView,
        result: Gio.AsyncResult,
        _user_data: object = None,
    ) -> None:
        text = ""
        href = ""
        try:
            value = web_view.evaluate_javascript_finish(result)
            if value is not None and value.is_string():
                data = json.loads(value.to_string())
                text = str(data.get("text") or "")
                href = str(data.get("href") or "")
        except Exception:
            log.debug("compose beginLink failed", exc_info=True)
        self._emit_link_request(text, href)

    def _emit_link_request(self, text: str, href: str) -> None:
        for handler in list(self._link_request_handlers):
            try:
                handler(text, href)
            except Exception:
                log.exception("compose link request handler failed")

    def _on_action_message(
        self,
        _manager: WebKit.UserContentManager,
        value: JavaScriptCore.Value,
    ) -> None:
        action = ""
        text = ""
        href = ""
        try:
            if value.is_object():
                action_v = value.object_get_property("action")
                text_v = value.object_get_property("text")
                href_v = value.object_get_property("href")
                if action_v is not None and action_v.is_string():
                    action = action_v.to_string()
                if text_v is not None and text_v.is_string():
                    text = text_v.to_string()
                if href_v is not None and href_v.is_string():
                    href = href_v.to_string()
        except Exception:
            log.debug("compose action message parse failed", exc_info=True)
            return
        if action == "link":
            self._emit_link_request(text, href)
        elif action == "open-link":
            self._open_href(href)

    def _open_href(self, href: str) -> None:
        uri = (href or "").strip()
        if not compose_uri_opens_externally(uri):
            return
        now = GLib.get_monotonic_time()
        # Click handlers (JS + WebKit navigation) can race with different
        # spellings of the same URL; drop repeats from one gesture.
        if (now - self._opened_uri_time) < 500_000:
            return
        parent = self._parent_window()
        if parent is None:
            return
        self._opened_uri_time = now
        open_uri_externally(parent, uri)

    def _parent_window(self) -> Gtk.Window | None:
        widget: Gtk.Widget | None = self
        while widget is not None:
            if isinstance(widget, Gtk.Window):
                return widget
            widget = widget.get_parent()
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

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
        self._clear_link_hover()
        self._refresh_format_mode_label()

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
        self._refresh_format_mode_label()
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
            if action is None:
                return False
            nav_type = action.get_navigation_type()
            request = action.get_request()
            uri = request.get_uri() if request is not None else ""
        except Exception:
            decision.ignore()
            return True
        # Allow the initial document load; open clicked links like the reader.
        if (
            decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION
            and nav_type == WebKit.NavigationType.OTHER
        ):
            return False
        if uri and compose_uri_opens_externally(uri):
            self._open_href(uri)
        decision.ignore()
        return True

    def _rebuild_spell_language_actions(self) -> None:
        for action in self._spell_language_actions.values():
            try:
                self._spell_action_group.remove_action(action.get_name())
            except Exception:
                pass
        self._spell_language_actions.clear()

        for code, _label in list_installed_spell_languages():
            name = action_name_for_language(code)
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._on_spell_language_activate, code)
            self._spell_action_group.add_action(action)
            self._spell_language_actions[code] = action

    def _on_spell_language_activate(
        self,
        _action: Gio.SimpleAction,
        _param: GLib.Variant | None,
        code: str,
    ) -> None:
        active = code in get_active_spell_languages()
        set_spell_language_active(code, not active)

    def _prepend_spell_language_menu(self, menu: WebKit.ContextMenu) -> None:
        languages = list_installed_spell_languages()
        if not languages:
            return

        active = set(get_active_spell_languages())
        submenu = WebKit.ContextMenu.new()
        for code, label in languages:
            action = self._spell_language_actions.get(code)
            if action is None:
                continue
            prefix = "✓ " if code in active else ""
            submenu.append(
                WebKit.ContextMenuItem.new_from_gaction(action, f"{prefix}{label}")
            )

        if not list(submenu.get_items()):
            return

        if list(menu.get_items()):
            menu.prepend(WebKit.ContextMenuItem.new_separator())
        menu.prepend(
            WebKit.ContextMenuItem.new_with_submenu(
                "Spelling Languages",
                submenu,
            )
        )

    def _on_context_menu(
        self,
        _web_view: WebKit.WebView,
        menu: WebKit.ContextMenu,
        hit_test_result: WebKit.HitTestResult,
    ) -> bool:
        # Strip navigation chrome; keep editing / spell-check items.
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
        if hit_test_result is not None and hit_test_result.context_is_link():
            if list(menu.get_items()):
                menu.prepend(WebKit.ContextMenuItem.new_separator())
            menu.prepend(
                WebKit.ContextMenuItem.new_from_gaction(
                    self._unlink_action, "Remove Link"
                )
            )
        self._prepend_spell_language_menu(menu)
        return False

    def _clear_link_hover(self) -> None:
        self._hover_link_uri = None
        apply_reader_link_hover(self._link_hover_box, self._link_hover_label, None)

    def _position_link_hover(
        self, pointer: tuple[float, float] | None = None
    ) -> None:
        if not self._link_hover_box.get_visible():
            return
        overlay = self._link_hover_overlay
        if pointer is None:
            pointer = pointer_coords_in_widget(overlay)
        if pointer is None:
            return
        _min_w, chip_w, _, _ = self._link_hover_box.measure(
            Gtk.Orientation.HORIZONTAL, -1
        )
        _min_h, chip_h, _, _ = self._link_hover_box.measure(
            Gtk.Orientation.VERTICAL, -1
        )
        x, y = reader_link_hover_origin(
            pointer[0],
            pointer[1],
            chip_w,
            chip_h,
            overlay.get_width(),
            overlay.get_height(),
        )
        self._link_hover_box.set_margin_start(max(0, int(round(x))))
        self._link_hover_box.set_margin_top(max(0, int(round(y))))

    def _on_mouse_target_changed(
        self,
        _web_view: WebKit.WebView,
        hit_test_result: WebKit.HitTestResult,
        _modifiers: int,
    ) -> None:
        href = reader_link_tooltip_text(hit_test_result)
        self._hover_link_uri = href
        if apply_reader_link_hover(
            self._link_hover_box,
            self._link_hover_label,
            href,
        ):
            self._position_link_hover()
