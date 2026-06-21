#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv --system-site-packages
  .venv/bin/pip install -e .
fi

install_desktop_integration() {
  local dest="$HOME/.local/share"
  local post_bin="$ROOT/.venv/bin/post"
  local desktop_file="$dest/applications/io.github.mbrennwa.Post.desktop"
  local icon_src="$ROOT/data/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg"
  local icon_png="$dest/icons/hicolor/128x128/apps/io.github.mbrennwa.Post.png"
  local broken_hicolor_theme="$dest/icons/hicolor/index.theme"
  local stale_svg="$dest/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg"
  local stale_adwaita_icon="$dest/icons/Adwaita/scalable/apps/io.github.mbrennwa.Post.svg"

  mkdir -p "$dest/applications"

  # A previous version wrote a minimal hicolor index.theme here, which shadowed
  # the system hicolor fallback and broke icons for Firefox and other apps.
  if [[ -f "$broken_hicolor_theme" ]] \
    && grep -q '^Directories=scalable/apps,scalable/actions$' "$broken_hicolor_theme"; then
    rm -f "$broken_hicolor_theme" "$dest/icons/hicolor/icon-theme.cache"
    echo "post: removed broken local hicolor icon theme — restart GNOME Shell if other app icons still look wrong" >&2
  fi

  rm -f "$stale_svg" "$stale_adwaita_icon"

  local icon_changed=0
  if [[ ! -f "$icon_src" ]]; then
    echo "post: missing icon source $icon_src" >&2
    exit 1
  fi
  if ! command -v rsvg-convert >/dev/null; then
    echo "post: rsvg-convert is required to install launcher icons" >&2
    exit 1
  fi

  # GNOME Shell's app switcher uses St, which often renders SVG app icons blank.
  # Install raster icons only and point the .desktop file at a PNG path.
  for size in 16 22 24 32 48 64 96 128 192 256; do
    local png_dir="$dest/icons/hicolor/${size}x${size}/apps"
    local png_installed="$png_dir/io.github.mbrennwa.Post.png"
    mkdir -p "$png_dir"
    if [[ ! -f "$png_installed" ]] || [[ "$icon_src" -nt "$png_installed" ]]; then
      icon_changed=1
      rsvg-convert -w "$size" -h "$size" "$icon_src" -o "$png_installed"
    fi
  done

  cat >"$desktop_file" <<EOF
[Desktop Entry]
Name=Post
GenericName=Email
Comment=Send and receive email
Icon=$icon_png
StartupWMClass=io.github.mbrennwa.Post
TryExec=$post_bin
Exec=$post_bin %U
Type=Application
Terminal=false
Categories=GNOME;GTK;Network;Email;
StartupNotify=true
EOF
  chmod 644 "$desktop_file"

  update-desktop-database "$dest/applications" 2>/dev/null || true
  if (( icon_changed )); then
    echo "post: icon updated — restart GNOME Shell if the launcher still shows the old icon" >&2
  fi
}

install_desktop_integration

exec .venv/bin/post "$@"
