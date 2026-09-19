# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression checks for the dedicated mail I/O thread architecture (#44, #425)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MAIL_PKG = _REPO_ROOT / "src" / "post" / "mail"
_UI_MAIL_MODULES = (
    _REPO_ROOT / "src" / "post" / "window.py",
    _REPO_ROOT / "src" / "post" / "sidebar.py",
    _REPO_ROOT / "src" / "post" / "compose_window.py",
    _REPO_ROOT / "src" / "post" / "reader_window.py",
    _REPO_ROOT / "src" / "post" / "mail" / "send_delay.py",
    _REPO_ROOT / "src" / "post" / "mail" / "offline_sync.py",
    _REPO_ROOT / "src" / "post" / "mail" / "sync_watcher.py",
)
# Timeout helper threads in offline_sync.py are allowed; mail I/O must not spawn its own.
_NO_MAIL_WORKER_THREAD_MODULES = (
    _REPO_ROOT / "src" / "post" / "window.py",
    _REPO_ROOT / "src" / "post" / "sidebar.py",
    _REPO_ROOT / "src" / "post" / "compose_window.py",
    _REPO_ROOT / "src" / "post" / "reader_window.py",
    _REPO_ROOT / "src" / "post" / "mail" / "send_delay.py",
    _REPO_ROOT / "src" / "post" / "mail" / "sync_watcher.py",
)


def _module_imports(name: str, path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def _uses_threading_thread(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "Thread":
            if isinstance(func.value, ast.Name) and func.value.id == "threading":
                return True
    return False


class MailThreadingContractTests(unittest.TestCase):
    def test_mail_package_does_not_import_smtplib(self) -> None:
        for path in _MAIL_PKG.rglob("*.py"):
            self.assertNotIn(
                "smtplib",
                _module_imports(path.stem, path),
                msg=f"{path.relative_to(_REPO_ROOT)} must not import smtplib",
            )

    def test_no_worker_session_symbols_in_eds(self) -> None:
        eds_source = (_MAIL_PKG / "eds.py").read_text(encoding="utf-8")
        for symbol in (
            "prepare_camel_worker_thread",
            "_camel_worker_tls",
            "_ensure_worker_session",
            "_get_worker_transport_unlocked",
        ):
            self.assertNotIn(
                symbol,
                eds_source,
                msg=f"eds.py must not define legacy worker symbol {symbol!r}",
            )

    def test_ui_mail_modules_do_not_spawn_threading_thread(self) -> None:
        for path in _NO_MAIL_WORKER_THREAD_MODULES:
            self.assertFalse(
                _uses_threading_thread(path),
                msg=(
                    f"{path.relative_to(_REPO_ROOT)} must use MailService "
                    "job methods, not threading.Thread"
                ),
            )

    def test_ui_mail_modules_do_not_call_get_mail_io_thread(self) -> None:
        for path in _UI_MAIL_MODULES:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "get_mail_io_thread",
                source,
                msg=(
                    f"{path.relative_to(_REPO_ROOT)} must call MailService "
                    "submit_interactive / *_async, not get_mail_io_thread"
                ),
            )

    def test_mail_threading_doc_exists(self) -> None:
        doc = _REPO_ROOT / "docs" / "mail-threading.md"
        self.assertTrue(doc.is_file(), "docs/mail-threading.md is required")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("post-mail-io", text)
        self.assertIn("Manual regression matrix", text)
        self.assertIn("submit_interactive", text)
        self.assertIn("job", text.lower())
        self.assertIn("shutdown_async", text)
        self.assertIn("connect_async", text)
        self.assertIn("camel-helper", text)
        self.assertIn("POST_MAIL_CAMEL_HELPER_PROCESS", text)

    def test_sidebar_archive_read_unflagged_counts_async(self) -> None:
        """GTK must not block on count before the confirm dialog (#443)."""
        source = (_REPO_ROOT / "src" / "post" / "sidebar.py").read_text(
            encoding="utf-8"
        )
        activate = source.split("def _on_archive_read_unflagged_activate", 1)[1]
        activate = activate.split("\n    def ", 1)[0]
        self.assertIn('submit_interactive("count_read_unflagged"', activate)
        self.assertNotIn(
            "confirm_action",
            activate,
            msg="confirm dialog must run after async count on GTK idle",
        )
        after = source.split("def _after_count_read_unflagged", 1)[1]
        after = after.split("\n    def ", 1)[0]
        self.assertIn("confirm_action", after)
        self.assertIn("archive_read_unflagged_messages", after)

    def test_window_quit_uses_shutdown_async(self) -> None:
        source = (_REPO_ROOT / "src" / "post" / "window.py").read_text(
            encoding="utf-8"
        )
        destroy = source.split("def _destroy_after_close_cleanup", 1)[1]
        destroy = destroy.split("\n    def ", 1)[0]
        self.assertIn("shutdown_async", destroy)
        self.assertNotIn("shutdown_sync", destroy)

    def test_app_connects_mail_async(self) -> None:
        source = (_REPO_ROOT / "src" / "post" / "app.py").read_text(encoding="utf-8")
        self.assertIn("connect_async", source)
        self.assertNotIn(
            "MailService.connect()",
            source,
            msg="app startup must use connect_async, not sync connect on GTK",
        )
