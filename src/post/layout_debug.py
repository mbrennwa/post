# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Optional GtkBox layout probes for diagnosing measure warnings."""

from __future__ import annotations

import logging
import os
import weakref

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk

log = logging.getLogger(__name__)

_DEFAULT_FOR_SIZE = 649


class _ProbeRegistry:
    def __init__(self) -> None:
        self._entries: list[tuple[str, weakref.ReferenceType[Gtk.Widget]]] = []

    def clear(self) -> None:
        self._entries.clear()

    def register(self, widget: Gtk.Widget, name: str) -> Gtk.Widget:
        self._entries.append((name, weakref.ref(widget)))
        return widget

    def targets(self) -> list[tuple[str, Gtk.Widget]]:
        alive: list[tuple[str, Gtk.Widget]] = []
        for name, widget_ref in self._entries:
            widget = widget_ref()
            if widget is not None:
                alive.append((name, widget))
        self._entries = [(name, weakref.ref(widget)) for name, widget in alive]
        return alive


_registry = _ProbeRegistry()


def enabled() -> bool:
    return os.environ.get("POST_DEBUG_LAYOUT", "").lower() in {"1", "true", "yes"}


def clear_registry() -> None:
    _registry.clear()


def name_widget(widget: Gtk.Widget, name: str) -> Gtk.Widget:
    widget.set_name(name)
    if enabled():
        _registry.register(widget, f"{name}@{id(widget):#x}")
    return widget


def _descendant_boxes(
    root: Gtk.Widget,
    *,
    prefix: str,
    max_depth: int,
) -> list[tuple[str, Gtk.Box]]:
    boxes: list[tuple[str, Gtk.Box]] = []

    def walk(widget: Gtk.Widget, depth: int, path: str) -> None:
        if depth > max_depth:
            return
        widget_name = widget.get_name() or type(widget).__name__
        child_path = f"{path}/{widget_name}" if path else widget_name
        if isinstance(widget, Gtk.Box):
            boxes.append((child_path, widget))
        child = widget.get_first_child()
        while child is not None:
            walk(child, depth + 1, child_path)
            child = child.get_next_sibling()

    walk(root, 0, prefix)
    return boxes


def _probe_targets(
    targets: list[tuple[str, Gtk.Box]],
    *,
    context: str,
    for_size: int,
) -> list[tuple[str, int, int]]:
    bad: list[tuple[str, int, int]] = []
    suffix = f" [{context}]" if context else ""

    for name, widget in targets:
        widget_type = type(widget).__name__
        minimum, natural, _, _ = widget.measure(Gtk.Orientation.HORIZONTAL, for_size)
        width = widget.get_width()
        height = widget.get_height()
        log.info(
            "layout probe%s: %s (%s) measure min=%d natural=%d for_size=%d size=%dx%d",
            suffix,
            name,
            widget_type,
            minimum,
            natural,
            for_size,
            width,
            height,
        )
        if natural < minimum:
            bad.append((name, minimum, natural))
            log.warning(
                "layout probe%s: GtkBox %s min width %d natural width %d for_size=%d",
                suffix,
                name,
                minimum,
                natural,
                for_size,
            )

    return bad


def probe_registered(
    *,
    context: str = "",
    for_size: int = _DEFAULT_FOR_SIZE,
) -> list[tuple[str, int, int]]:
    """Probe registered boxes and their named Gtk.Box descendants."""
    registered = _registry.targets()
    if not registered:
        suffix = f" [{context}]" if context else ""
        log.info("layout probe%s: no registered probe targets", suffix)
        return []

    unique_names = sorted({name.split("@", 1)[0] for name, _ in registered})
    suffix = f" [{context}]" if context else ""
    log.info(
        "layout probe%s: checking %d registered roots (%s)",
        suffix,
        len(registered),
        ", ".join(unique_names),
    )

    targets: list[tuple[str, Gtk.Box]] = []
    seen_ids: set[int] = set()
    for name, widget in registered:
        if not isinstance(widget, Gtk.Box):
            continue
        widget_id = id(widget)
        if widget_id not in seen_ids:
            seen_ids.add(widget_id)
            targets.append((name, widget))
        root_name = name.split("@", 1)[0]
        for child_name, child in _descendant_boxes(
            widget,
            prefix=root_name,
            max_depth=6,
        ):
            child_id = id(child)
            if child_id in seen_ids:
                continue
            seen_ids.add(child_id)
            targets.append((f"{child_name}@{child_id:#x}", child))

    log.info(
        "layout probe%s: expanded to %d Gtk.Box targets",
        suffix,
        len(targets),
    )
    return _probe_targets(targets, context=context, for_size=for_size)


def schedule_probe(*, context: str = "", for_size: int = _DEFAULT_FOR_SIZE) -> None:
    if not enabled():
        return

    def idle() -> bool:
        probe_registered(context=context, for_size=for_size)
        return False

    GLib.idle_add(idle)


# Backwards-compatible helper for unit tests.
def probe_boxes(
    root: Gtk.Widget,
    *,
    for_size: int,
    context: str = "",
) -> list[tuple[str, str, int, int]]:
    if not isinstance(root, Gtk.Box):
        return []
    minimum, natural, _, _ = root.measure(Gtk.Orientation.HORIZONTAL, for_size)
    name = root.get_name() or type(root).__name__
    if natural < minimum:
        suffix = f" [{context}]" if context else ""
        log.warning(
            "layout probe%s: GtkBox %s min width %d natural width %d for_size=%d",
            suffix,
            name,
            minimum,
            natural,
            for_size,
        )
        return [(name, name, minimum, natural)]
    return []
