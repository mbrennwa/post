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
        "Add a recipient in the To field",
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
        "Inboxes",
        "Display",
        "Outgoing",
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
        "OFFLINE_CACHED_LIST_STATUS",
        "OFFLINE_SEARCHING_LOCAL_CACHE",
        "OFFLINE_CACHE_STATUS_PREFIX",
        "MESSAGE_LIST_SYNC_STATUS",
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
    "set_title": 0,
}

_SENTENCE_CALLS: dict[str, int] = {
    "_set_status": 0,
    "set_body": 0,
}

# Gtk.Label / button targets whose set_label text uses header capitalization.
_HEADER_SET_LABEL_RECEIVERS = frozenset(
    {
        "_message_empty_label",
        "_message_loading_label",
    }
)

# Gtk.Label targets whose set_label text uses sentence capitalization.
_SENTENCE_SET_LABEL_RECEIVERS = frozenset(
    {
        "_status",
        "_reader_subject",
        "_reader_meta",
        "_message_error_label",
    }
)

# Local variables assigned UI strings later passed to header-style labels.
_HEADER_LABEL_VARIABLES = frozenset({"loading_label"})

# Local variables assigned UI strings later passed to the status bar.
_SENTENCE_STATUS_VARIABLES = frozenset({"preparing", "queued_status"})

# Status-bar helper functions whose return templates are checked at runtime.
_STATUS_MESSAGE_FUNCTIONS: dict[str, frozenset[str]] = {
    "mail/folders.py": frozenset(
        {
            "format_folder_refresh_start",
            "format_folder_refresh_done",
            "format_folder_refresh_error",
            "format_account_refresh_start",
            "format_account_refresh_done",
            "format_account_refresh_error",
        }
    ),
    "mail/send_queue.py": frozenset(
        {
            "offline_status_text",
            "offline_cache_status_text",
        }
    ),
    "mail/operation_queue.py": frozenset({"offline_queue_status_text"}),
    "window.py": frozenset(
        {
            "_loading_progress_text",
            "_move_status_label",
        }
    ),
}

# Substitute for {…} placeholders when checking f-string UI templates.
_FSTRING_PLACEHOLDER = "Inbox"


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


def _attribute_chain_leaf(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _fstring_sample(node: ast.JoinedStr) -> str | None:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append(_FSTRING_PLACEHOLDER)
        else:
            return None
    result = "".join(parts)
    return result or None


def _collect_literal_ui_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        sample = _fstring_sample(node)
        return [sample] if sample else []
    if isinstance(node, ast.IfExp):
        return _collect_literal_ui_strings(node.body) + _collect_literal_ui_strings(
            node.orelse
        )
    return []


def _is_header_set_label_receiver(receiver: str | None) -> bool:
    if receiver is None:
        return False
    if receiver in _HEADER_SET_LABEL_RECEIVERS:
        return True
    return receiver.endswith("_btn")


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
        elif isinstance(target, ast.Name) and target.id in _HEADER_LABEL_VARIABLES:
            for text in _collect_literal_ui_strings(node.value):
                self._record("header", text, node.lineno)
        elif isinstance(target, ast.Name) and target.id in _SENTENCE_STATUS_VARIABLES:
            for text in _collect_literal_ui_strings(node.value):
                self._record("sentence", text, node.lineno)
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
                arg = node.args[index]
                if (
                    call_name == "_set_status"
                    and isinstance(arg, ast.Call)
                    and _call_name(arg) == "_with_load_status_detail"
                    and arg.args
                ):
                    arg = arg.args[0]
                for text in _collect_literal_ui_strings(arg):
                    self._record("sentence", text, node.lineno)

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

        if widget_name in {
            "Gtk.Button",
            "Adw.SwitchRow",
            "Adw.EntryRow",
            "Adw.ComboRow",
            "Adw.ActionRow",
        }:
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

        if call_name == "set_label" and node.args:
            receiver = (
                _attribute_chain_leaf(node.func.value)
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if _is_header_set_label_receiver(receiver):
                for text in _collect_literal_ui_strings(node.args[0]):
                    self._record("header", text, node.lineno)
            elif receiver in _SENTENCE_SET_LABEL_RECEIVERS:
                for text in _collect_literal_ui_strings(node.args[0]):
                    self._record("sentence", text, node.lineno)

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


def _collect_status_function_strings(
    tree: ast.AST, relative_path: str
) -> list[tuple[str, int]]:
    function_names = _STATUS_MESSAGE_FUNCTIONS.get(relative_path)
    if function_names is None:
        return []
    collected: list[tuple[str, int]] = []

    class _StatusFunctionVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._active = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node.name not in function_names:
                self.generic_visit(node)
                return
            previous = self._active
            self._active = True
            self.generic_visit(node)
            self._active = previous

        def visit_Return(self, node: ast.Return) -> None:
            if self._active and node.value is not None:
                for text in _collect_literal_ui_strings(node.value):
                    collected.append((text, node.lineno))
            self.generic_visit(node)

    _StatusFunctionVisitor().visit(tree)
    return collected


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
        for text, lineno in _collect_status_function_strings(tree, relative_path):
            collected.append((relative_path, "sentence", text, lineno))
    for relative_path in _STATUS_MESSAGE_FUNCTIONS:
        if relative_path in _UI_MODULES:
            continue
        path = _SRC_ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for text, lineno in _collect_status_function_strings(tree, relative_path):
            collected.append((relative_path, "sentence", text, lineno))
    return collected


def _runtime_status_message_samples() -> list[str]:
    from post.mail.folders import (
        format_account_refresh_done,
        format_account_refresh_error,
        format_account_refresh_start,
        format_folder_refresh_done,
        format_folder_refresh_error,
        format_folder_refresh_start,
    )
    from post.mail.operation_queue import offline_queue_status_text
    from post.mail.send_queue import offline_cache_status_text, offline_status_text
    from post.window import MainWindow

    samples = [
        format_folder_refresh_start("Inbox"),
        format_folder_refresh_done("Sent", 2, 10),
        format_folder_refresh_error("Trash"),
        format_account_refresh_start("Work"),
        format_account_refresh_done("Work", 1),
        format_account_refresh_done("Work", 4),
        format_account_refresh_error("Work"),
        offline_status_text(queued_count=0),
        offline_status_text(queued_count=1),
        offline_status_text(queued_count=3),
        offline_cache_status_text(account_label="Work", folder_name="Inbox"),
        offline_cache_status_text(account_label="Work", folder_name=""),
        offline_queue_status_text(
            send_queued_count=0,
            operation_queued_count=0,
        ),
        offline_queue_status_text(
            send_queued_count=1,
            operation_queued_count=2,
        ),
        offline_queue_status_text(
            send_queued_count=2,
            operation_queued_count=0,
            draft_queued_count=1,
        ),
        MainWindow._loading_progress_text(
            MainWindow,
            "Inbox",
            searching=False,
            source="server",
        ),
        MainWindow._move_status_label(MainWindow, "trash", 1),
        MainWindow._move_status_label(MainWindow, "archive", 3),
    ]
    return samples


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
            "Offline · showing cached list",
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
    def test_collects_preference_group_titles_and_fstring_labels(self) -> None:
        collected = {
            (module, text)
            for module, _style, text, _lineno in _collect_ui_strings()
        }
        self.assertIn(("settings_window.py", "Load Remote Content"), collected)
        self.assertIn(("settings_window.py", "No Sendable Accounts"), collected)
        self.assertIn(("window.py", "No Messages in Inbox"), collected)

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

    def test_runtime_status_messages_follow_sentence_capitalization(self) -> None:
        failures: list[str] = []
        for sample in _runtime_status_message_samples():
            if not is_sentence_capitalized(sample):
                failures.append(
                    f"runtime status message {sample!r} should start with a "
                    "capital letter and capitalize each new sentence"
                )
        if failures:
            self.fail("\n".join(failures))
