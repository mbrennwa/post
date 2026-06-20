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
  local icon_installed="$dest/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg"
  local icon_dir="$dest/icons/hicolor/scalable/apps"

  mkdir -p "$dest/applications" "$icon_dir"

  local icon_changed=0
  if [[ ! -f "$icon_installed" ]] || ! cmp -s "$icon_src" "$icon_installed"; then
    icon_changed=1
    cp -f "$icon_src" "$icon_installed"
  fi

  cat >"$desktop_file" <<EOF
[Desktop Entry]
Name=Post
GenericName=Email
Comment=Send and receive email
Icon=$icon_src
TryExec=$post_bin
Exec=$post_bin %U
Type=Application
Terminal=false
Categories=GNOME;GTK;Network;Email;
StartupNotify=true
EOF
  chmod 644 "$desktop_file"

  if [[ ! -f "$dest/icons/hicolor/index.theme" ]]; then
    cat >"$dest/icons/hicolor/index.theme" <<'EOF'
[Icon Theme]
Name=Hicolor
Comment=Fallback icon theme
Directories=scalable/apps

[scalable/apps]
Size=256
Type=Scalable
MinSize=1
MaxSize=512
EOF
  fi

  if (( icon_changed )); then
    rm -f "$dest/icons/hicolor/icon-theme.cache"
    gtk-update-icon-cache -f "$dest/icons/hicolor" 2>/dev/null || true
    update-desktop-database "$dest/applications" 2>/dev/null || true
    echo "post: icon updated — restart GNOME Shell if the launcher still shows the old icon" >&2
  else
    update-desktop-database "$dest/applications" 2>/dev/null || true
  fi
}

install_desktop_integration

exec .venv/bin/post "$@"
