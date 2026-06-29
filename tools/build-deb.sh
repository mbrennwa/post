#!/usr/bin/env bash
set -euo pipefail

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "WARNING: git working tree has uncommitted changes."
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f debian/rules ]; then
  chmod +x debian/rules
fi

VERSION="$(python3 - <<'EOF'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
EOF
)"

CHANGELOG_VERSION="$(head -1 debian/changelog | sed -n 's/^[^ (]* (\([^)]*\)).*/\1/p')"
CHANGELOG_UPSTREAM="${CHANGELOG_VERSION%%-*}"

if [ "$CHANGELOG_UPSTREAM" != "$VERSION" ]; then
  echo "ERROR: debian/changelog ($CHANGELOG_VERSION) != pyproject.toml ($VERSION)" >&2
  echo "Update debian/changelog when bumping [project].version." >&2
  exit 1
fi

echo "Building post version: $VERSION"

python3 - <<'PY'
from __future__ import annotations

from pathlib import Path

import tomllib

cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
deb = cfg.get("tool", {}).get("deb", {})

apt_depends = deb.get("apt_depends", [])
if not apt_depends:
    raise SystemExit("ERROR: [tool.deb].apt_depends is missing or empty in pyproject.toml")

control = Path("debian/control")
lines = control.read_text(encoding="utf-8").splitlines(True)

out: list[str] = []
in_pkg = False
depends_written = False
skip_depends_continuation = False
for line in lines:
    if line.startswith("Package:"):
        in_pkg = line.strip() == "Package: post"
        depends_written = False if in_pkg else depends_written

    if skip_depends_continuation:
        if line.startswith((" ", "\t")) and not line.strip().startswith("Description:"):
            continue
        skip_depends_continuation = False

    if in_pkg and line.startswith("Depends:"):
        joined = ", ".join(apt_depends)
        out.append("Depends: ${misc:Depends},\n")
        out.append("          " + joined.replace(", ", ",\n          ") + "\n")
        depends_written = True
        skip_depends_continuation = True
        continue

    out.append(line)

if not depends_written:
    raise SystemExit("ERROR: Could not find 'Depends:' line in debian/control for Package: post")

control.write_text("".join(out), encoding="utf-8")
print("Updated debian/control Depends for Package: post")
PY

dpkg-buildpackage -us -uc

DEB="post_${VERSION}_all.deb"
rm -rf dist
mkdir -p dist
if [ ! -f "../$DEB" ]; then
  echo "ERROR: expected ../$DEB after build" >&2
  exit 1
fi
mv -f "../$DEB" "dist/$DEB"
rm -f ../post_*.changes ../post_*.buildinfo ../post_*.dsc ../post_*.tar.* 2>/dev/null || true

echo "Wrote dist/$DEB"
