#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv --system-site-packages
  .venv/bin/pip install -e .
fi

install_desktop_integration() {
  local dest="$HOME/.local/share"
  mkdir -p "$dest/applications" "$dest/icons/hicolor/scalable/apps"
  install -Dm644 data/io.github.mbrennwa.Post.desktop "$dest/applications/"
  install -Dm644 data/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg \
    "$dest/icons/hicolor/scalable/apps/"
  update-desktop-database "$dest/applications" 2>/dev/null || true
  gtk-update-icon-cache "$dest/icons/hicolor" 2>/dev/null || true
}

if [[ ! -f "$HOME/.local/share/applications/io.github.mbrennwa.Post.desktop" ]]; then
  install_desktop_integration
fi

exec .venv/bin/post "$@"
