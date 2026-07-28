# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Persistent application logging for diagnostics (#202).

Writes timestamped warnings, errors, and notable operational events to an
on-disk rotating log under XDG state. Does not log message bodies, passwords,
OAuth tokens, or full MIME.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
_BACKUP_COUNT = 3
_FILE_HANDLER_NAME = "post.file"
_STREAM_HANDLER_NAME = "post.stream"

_configured = False
_excepthook_installed = False
_glib_bridge_installed = False
_in_glib_bridge = False

# Domains that commonly emit Gtk/Camel diagnostics useful for bug reports.
_GLIB_LOG_DOMAINS: tuple[str | None, ...] = (
    None,
    "Gtk",
    "Gdk",
    "GLib",
    "GLib-GObject",
    "Gio",
    "Pango",
    "Adwaita",
    "Camel",
    "evolution-data-server",
    "libebook",
    "libedata-book",
)


def log_dir() -> Path:
    """Return the XDG state directory used for Post logs."""
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        return Path(xdg_state) / "post"
    return Path.home() / ".local" / "state" / "post"


def log_file_path() -> Path:
    """Return the primary application log file path."""
    return log_dir() / "post.log"


def open_log_file_uri() -> str:
    """Return a ``file://`` URI for the application log file."""
    return log_file_path().resolve().as_uri()


def _parse_env_level() -> int | None:
    name = os.environ.get("POST_LOG_LEVEL", "").strip()
    if not name:
        return None
    return int(getattr(logging, name.upper(), logging.DEBUG))


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT)


def _install_excepthook() -> None:
    global _excepthook_installed
    if _excepthook_installed:
        return
    _excepthook_installed = True
    previous = sys.excepthook
    logger = logging.getLogger("post")

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: Any,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = _hook


def _glib_level_to_logging(level_flags: int) -> int:
    # GLib packs level bits; match the highest-severity bit we care about.
    try:
        from gi.repository import GLib

        flags = GLib.LogLevelFlags
        if level_flags & int(flags.LEVEL_ERROR):
            return logging.ERROR
        if level_flags & int(flags.LEVEL_CRITICAL):
            return logging.CRITICAL
        if level_flags & int(flags.LEVEL_WARNING):
            return logging.WARNING
    except Exception:
        pass
    return logging.WARNING


def _install_glib_bridge() -> None:
    """Forward GLib/Gtk WARNING+ messages into Python logging (best-effort)."""
    global _glib_bridge_installed
    if _glib_bridge_installed:
        return
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
    except Exception:
        return

    levels = (
        GLib.LogLevelFlags.LEVEL_WARNING
        | GLib.LogLevelFlags.LEVEL_CRITICAL
        | GLib.LogLevelFlags.LEVEL_ERROR
    )

    def _on_glib_log(
        domain: str | None,
        level_flags: GLib.LogLevelFlags,
        message: str,
        _user_data: Any,
    ) -> None:
        global _in_glib_bridge
        if _in_glib_bridge:
            return
        _in_glib_bridge = True
        try:
            name = f"glib.{domain}" if domain else "glib"
            py_level = _glib_level_to_logging(int(level_flags))
            logging.getLogger(name).log(py_level, "%s", message)
            # Keep journal/stderr output from the default GLib handler.
            GLib.log_default_handler(domain, level_flags, message, None)
        finally:
            _in_glib_bridge = False

    try:
        for domain in _GLIB_LOG_DOMAINS:
            GLib.log_set_handler(domain, levels, _on_glib_log, None)
        _glib_bridge_installed = True
    except Exception:
        # Best-effort: leave Python logging working even if GI handlers fail.
        return


def configure_logging() -> Path:
    """Configure root logging with rotating file + stderr handlers.

    Idempotent. Returns the log file path.
    """
    global _configured
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    env_level = _parse_env_level()
    if env_level is not None:
        root_level = env_level
        file_level = env_level
        stream_level = env_level
    else:
        root_level = logging.INFO
        file_level = logging.INFO
        stream_level = logging.WARNING

    root = logging.getLogger()
    root.setLevel(root_level)

    if not _configured:
        formatter = _make_formatter()

        # Drop leftover handlers from prior basicConfig / test runs so we own
        # the configuration, but only when we have not configured yet.
        existing_names = {
            getattr(handler, "name", None) for handler in root.handlers
        }
        if (
            _FILE_HANDLER_NAME not in existing_names
            or _STREAM_HANDLER_NAME not in existing_names
        ):
            file_handler = RotatingFileHandler(
                path,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.set_name(_FILE_HANDLER_NAME)
            file_handler.setLevel(file_level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

            stream_handler = logging.StreamHandler()
            stream_handler.set_name(_STREAM_HANDLER_NAME)
            stream_handler.setLevel(stream_level)
            stream_handler.setFormatter(formatter)
            root.addHandler(stream_handler)

        _configured = True
        _install_excepthook()
        _install_glib_bridge()
        logging.getLogger("post").info("Logging to %s", path)
    else:
        # Refresh levels if POST_LOG_LEVEL changed between calls (tests).
        for handler in root.handlers:
            name = getattr(handler, "name", None)
            if name == _FILE_HANDLER_NAME:
                handler.setLevel(file_level)
            elif name == _STREAM_HANDLER_NAME:
                handler.setLevel(stream_level)

    return path


def _reset_for_tests() -> None:
    """Clear handler state so unit tests can reconfigure cleanly.

    Leaves the GLib bridge and excepthook installed (process-global, safe to
    keep once) so tests do not stack duplicate GLib handlers.
    """
    global _configured, _in_glib_bridge
    root = logging.getLogger()
    for handler in list(root.handlers):
        name = getattr(handler, "name", None)
        if name in {_FILE_HANDLER_NAME, _STREAM_HANDLER_NAME}:
            root.removeHandler(handler)
            handler.close()
    _configured = False
    _in_glib_bridge = False
