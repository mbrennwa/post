Name:           post
Version:        1.0.0a1
Release:        1
Summary:        Post — simple GNOME mail client
License:        GPL-3.0-or-later
URL:            https://github.com/mbrennwa/post
Source0:        post-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools

# Runtime Requires are rewritten by tools/build-rpm.sh from
# [tool.rpm].dnf_requires in pyproject.toml.
Requires:       python3 >= 3.10
Requires:       python3-pip
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       glib2
Requires:       webkitgtk6.0
Requires:       evolution-data-server
Requires:       evolution-ews
Requires:       librsvg2-tools
Requires:       desktop-file-utils
Recommends:      gnome-online-accounts

%description
Post is a mail app for GNOME — read and send email with a native desktop
experience. It uses Evolution Data Server and GNOME Online Accounts.

%prep
%setup -q

%build
# Wheelhouse is built in %%install so the packaged wheels match the install root.

%install
set -eu
rm -rf %{buildroot}

# Ship an offline wheelhouse; the venv is created on the target machine
# (%%post) so the Python minor version matches the target system.
WHEEL_DIR="%{buildroot}/usr/lib/post/wheels"
BUILD_VENV="%{_builddir}/post-%{version}-build-venv"
export PYTHONNOUSERSITE=1

rm -rf "$WHEEL_DIR" "$BUILD_VENV"
mkdir -p "$WHEEL_DIR"

python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --upgrade pip wheel setuptools
"$BUILD_VENV/bin/python" -m pip wheel --no-deps --wheel-dir "$WHEEL_DIR" .

rm -rf "$BUILD_VENV"

install -Dm755 debian/wrapper/post %{buildroot}/usr/bin/post
install -Dm755 debian/wrapper/post-probe %{buildroot}/usr/bin/post-probe

install -Dm644 data/io.github.mbrennwa.Post.desktop \
	%{buildroot}/usr/share/applications/io.github.mbrennwa.Post.desktop
for size in 48 96 128 192 256; do
	install -Dm644 \
		"data/icons/hicolor/${size}x${size}/apps/io.github.mbrennwa.Post.png" \
		"%{buildroot}/usr/share/icons/hicolor/${size}x${size}/apps/io.github.mbrennwa.Post.png"
done
install -Dm644 data/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg \
	%{buildroot}/usr/share/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg

%post
set -eu

VENV=/usr/lib/post/venv
WHEELS=/usr/lib/post/wheels

rm -rf "$VENV"
python3 -m venv --system-site-packages "$VENV"

"$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true

"$VENV/bin/python" -m pip install \
  --no-index \
  --find-links "$WHEELS" \
  --no-deps \
  post

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications || true
fi

%postun
set -eu
# $1 == 0 means the package is being removed (not upgraded).
if [ "$1" -eq 0 ]; then
  rm -rf /usr/lib/post/venv
fi

%files
%license LICENSE
%doc README.md
/usr/bin/post
/usr/bin/post-probe
/usr/lib/post/wheels/
/usr/share/applications/io.github.mbrennwa.Post.desktop
/usr/share/icons/hicolor/*/apps/io.github.mbrennwa.Post.png
/usr/share/icons/hicolor/scalable/apps/io.github.mbrennwa.Post.svg

%changelog
* Thu Aug 06 2026 Matthias Brennwald <mbrennwa@gmail.com> - 1.0.0a1-1
- Bump to 1.0.0a1 (public alpha).

* Sun Aug 02 2026 Matthias Brennwald <mbrennwa@gmail.com> - 1.0.0.dev2-1
- Bump to 1.0.0.dev2 for Release_testing-2 WIP.

* Sun Aug 02 2026 Matthias Brennwald <mbrennwa@gmail.com> - 1.0.0.dev1-1
- Bump to 1.0.0.dev1 for Release_testing-1.

* Sat Aug 01 2026 Matthias Brennwald <mbrennwa@gmail.com> - 0.1.0-1
- Initial RPM package.
