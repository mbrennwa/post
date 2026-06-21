# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from post.mail.helpers import flag_menu_label, read_menu_label
from post.ui_capitalization import (
    heading_capitalization_style,
    is_header_capitalized,
    is_sentence_capitalized,
    to_header_capitalization,
)

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "post"

_UI_MODULES = (
    "compose_window.py",
    "window.py",
    "sidebar.py",
    "settings_window.py",
    "credentials.py",
    "folder_dialogs.py",
    "mail/helpers.py",
    "mail/send_queue.py",
    "reader/html.py",
    "mail/accounts.py",
)

# Literal UI strings that intentionally break the generic rules.
_EXEMPT_STRINGS = frozenset(
    {
        "(no subject)",
        "Search…  from: to: subject: …",
        "recipient@example.com",
        "Add a recipient in the To field.",
        "Subject is required",
        "Optional",
        "Password",
        "Folder name",
        "OK",
        "Cc",
        "Bcc",
        "From",
        "To",
        "Subject",
        "Account",
        "Path",
        "Storage",
        "Cancel",
        "Send",
        "Delete",
        "Archive",
        "Reply",
        "Forward",
        "Draft",
        "Discard",
        "Rename",
        "Create",
        "Confirm",
        "Refresh",
        "Undo",
        "Flag",
        "Unflag",
        "Settings",
        "Inbox",
        "Reading",
        "Composing",
        "Attachments",
        "Post",
        "Email",
        "Save...",
        "Enable mail at ~/.local/share/evolution/mail/local",
    }
)

_SENTENCE_CONSTANTS = frozenset(
    {
        "OFFLINE_MAIL_MESSAGE",
        "OFFLINE_FOLDER_MESSAGE",
    }
)

_HEADER_CONSTANTS = frozenset(
    {
        "EDS_LOCAL_DISPLAY_NAME",
    }
)

_HEADER_KEYWORDS = frozenset(
    {
        "confirm_label",
        "title",
        "label",
    }
)

_SENTENCE_KEYWORDS = frozenset(
    {
        "body",
        "subtitle",
        "error_heading",
    }
)

_HEADER_CALLS: dict[str, int] = {
    "set_tooltip_text": 0,
    "add_response": 1,
}

_SENTENCE_CALLS: dict[str, int] = {
    "_set_status": 0,
    "set_body": 0,
}


def _is_string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _gtk_widget_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name):
            return f"{value.id}.{func.attr}"
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return f"{value.value.id}.{value.attr}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return None


class _UIStringCollector(ast.NodeVisitor):
    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.strings: list[tuple[str, str, int]] = []

    def _record(self, style: str, text: str, lineno: int) -> None:
        if not text or text in _EXEMPT_STRINGS:
            return
        self.strings.append((style, text, lineno))

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) != 1:
            self.generic_visit(node)
            return
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            self.generic_visit(node)
            return
        if target.id in _SENTENCE_CONSTANTS:
            value = _is_string_literal(node.value)
            if value is not None:
                self._record("sentence", value, node.lineno)
            elif isinstance(node.value, ast.Tuple):
                parts = [
                    _is_string_literal(element)
                    for element in node.value.elts
                ]
                if all(part is not None for part in parts):
                    self._record("sentence", "".join(parts), node.lineno)
        elif target.id in _HEADER_CONSTANTS:
            value = _is_string_literal(node.value)
            if value is not None:
                self._record("header", value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node)
        widget_name = _gtk_widget_name(node)

        if call_name in _HEADER_CALLS:
            index = _HEADER_CALLS[call_name]
            if index < len(node.args):
                value = _is_string_literal(node.args[index])
                if value is not None:
                    self._record("header", value, node.lineno)

        if call_name in _SENTENCE_CALLS:
            index = _SENTENCE_CALLS[call_name]
            if index < len(node.args):
                value = _is_string_literal(node.args[index])
                if value is not None:
                    self._record("sentence", value, node.lineno)

        if call_name == "append" and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if isinstance(receiver, ast.Name) and receiver.id in {
                "items",
                "model",
                "results",
                "updates",
                "changed_uids",
                "messages",
                "accounts",
            }:
                self.generic_visit(node)
                return

        if call_name == "set_heading":
            if node.args:
                value = _is_string_literal(node.args[0])
                if value is not None:
                    style = heading_capitalization_style(value)
                    self._record(style, value, node.lineno)

        if widget_name in {"Gtk.Button", "Adw.SwitchRow", "Adw.EntryRow", "Adw.ComboRow"}:
            for keyword in node.keywords:
                if keyword.arg in _HEADER_KEYWORDS:
                    value = _is_string_literal(keyword.value)
                    if value is not None:
                        self._record("header", value, node.lineno)

        if widget_name == "Gtk.Label":
            for keyword in node.keywords:
                if keyword.arg == "label":
                    value = _is_string_literal(keyword.value)
                    if value is not None and value:
                        self._record("header", value, node.lineno)

        if widget_name in {"Adw.AlertDialog", "Adw.MessageDialog"}:
            for keyword in node.keywords:
                if keyword.arg == "heading":
                    value = _is_string_literal(keyword.value)
                    if value is not None:
                        style = heading_capitalization_style(value)
                        self._record(style, value, node.lineno)
                elif keyword.arg in _SENTENCE_KEYWORDS:
                    value = _is_string_literal(keyword.value)
                    if value is not None:
                        self._record("sentence", value, node.lineno)
                elif keyword.arg == "confirm_label":
                    value = _is_string_literal(keyword.value)
                    if value is not None:
                        self._record("header", value, node.lineno)

        if call_name == "append" and isinstance(node.func, ast.Attribute):
            if node.args:
                value = _is_string_literal(node.args[0])
                if value is not None and _looks_like_menu_label(value):
                    self._record("header", value, node.lineno)

        if widget_name == "Gtk.FileDialog":
            for keyword in node.keywords:
                if keyword.arg == "title":
                    value = _is_string_literal(keyword.value)
                    if value is not None:
                        self._record("header", value, node.lineno)

        for keyword in node.keywords:
            if keyword.arg == "heading":
                value = _is_string_literal(keyword.value)
                if value is not None:
                    style = heading_capitalization_style(value)
                    self._record(style, value, node.lineno)
            elif keyword.arg in _SENTENCE_KEYWORDS:
                value = _is_string_literal(keyword.value)
                if value is not None:
                    self._record("sentence", value, node.lineno)
            elif keyword.arg == "confirm_label":
                value = _is_string_literal(keyword.value)
                if value is not None:
                    self._record("header", value, node.lineno)

        self.generic_visit(node)


def _looks_like_menu_label(text: str) -> bool:
    if text.startswith(("win.", "sidebar.")):
        return False
    if text.islower() and " " not in text:
        return False
    return True


def _collect_ui_strings() -> list[tuple[str, str, str, int]]:
    collected: list[tuple[str, str, str, int]] = []
    for relative_path in _UI_MODULES:
        path = _SRC_ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = _UIStringCollector(relative_path)
        visitor.visit(tree)
        for style, text, lineno in visitor.strings:
            collected.append((relative_path, style, text, lineno))
    return collected


class HeaderCapitalizationHelperTests(unittest.TestCase):
    def test_header_examples(self) -> None:
        cases = {
            "Save Draft": "Save draft",
            "Load More": "Load more",
            "Mark as Read": "Mark as read",
            "Move to Trash": "Move to Trash",
            "No Message Selected": "No Message Selected",
            "New Message": "New Message",
            "Rename Folder": "Rename Folder",
            "Empty Trash": "Empty Trash",
            "Load Remote Content": "Load remote content",
            "Sign In": "Sign In",
            "Hide Cc": "Hide Cc",
        }
        for expected, sample in cases.items():
            with self.subTest(sample=sample):
                self.assertEqual(to_header_capitalization(sample), expected)
                self.assertTrue(is_header_capitalized(expected))

    def test_header_rejects_sentence_case(self) -> None:
        self.assertFalse(is_header_capitalized("Save draft"))
        self.assertFalse(is_header_capitalized("No messages"))


class SentenceCapitalizationHelperTests(unittest.TestCase):
    def test_sentence_examples(self) -> None:
        samples = (
            "Draft saved",
            "Enter a name for the new folder.",
            "Moved message to Trash",
            "Could not save settings",
            "You're offline. Messages will load when you reconnect.",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(is_sentence_capitalized(sample))

    def test_sentence_proper_nouns(self) -> None:
        self.assertTrue(
            is_sentence_capitalized(
                "Save your changes to Drafts before closing?"
            )
        )


class MenuLabelCapitalizationTests(unittest.TestCase):
    def test_read_menu_labels_use_header_capitalization(self) -> None:
        self.assertEqual(read_menu_label("read", 3), "Mark as Read (3)")
        self.assertEqual(read_menu_label("unread", 1), "Mark as Unread")
        self.assertTrue(is_header_capitalized(read_menu_label("read", 0)))

    def test_flag_menu_labels_use_header_capitalization(self) -> None:
        self.assertTrue(is_header_capitalized(flag_menu_label("flag", 2)))
        self.assertTrue(is_header_capitalized(flag_menu_label("unflag", 0)))


class UISourceCapitalizationTests(unittest.TestCase):
    def test_ui_source_strings_follow_gnome_capitalization(self) -> None:
        failures: list[str] = []
        for module, style, text, lineno in _collect_ui_strings():
            if style == "header" and not is_header_capitalized(text):
                expected = to_header_capitalization(text)
                failures.append(
                    f"{module}:{lineno}: header string {text!r} "
                    f"should be {expected!r}"
                )
            elif style == "sentence" and not is_sentence_capitalized(text):
                failures.append(
                    f"{module}:{lineno}: sentence string {text!r} "
                    "should start with a capital letter and capitalize "
                    "each new sentence"
                )
        if failures:
            self.fail("\n".join(failures))
