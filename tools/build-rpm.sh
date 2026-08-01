#!/usr/bin/env bash
set -euo pipefail

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "WARNING: git working tree has uncommitted changes."
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

for cmd in rpmbuild python3 tar; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $cmd" >&2
    echo "See tools/howto-build-rpm.txt for build-machine prerequisites." >&2
    exit 1
  fi
done

VERSION="$(python3 - <<'EOF'
from pathlib import Path
import tomllib

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
print(data["project"]["version"])
EOF
)"

SPEC_VERSION="$(sed -n 's/^Version:[[:space:]]*//p' rpm/post.spec | head -1)"
if [ "$SPEC_VERSION" != "$VERSION" ]; then
  echo "ERROR: rpm/post.spec Version ($SPEC_VERSION) != pyproject.toml ($VERSION)" >&2
  echo "Update rpm/post.spec Version (and %%changelog) when bumping [project].version." >&2
  exit 1
fi

echo "Building post version: $VERSION"

python3 - <<'PY'
from __future__ import annotations

from pathlib import Path

import tomllib

cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
rpm = cfg.get("tool", {}).get("rpm", {})

dnf_requires = rpm.get("dnf_requires", [])
if not dnf_requires:
    raise SystemExit("ERROR: [tool.rpm].dnf_requires is missing or empty in pyproject.toml")

spec_path = Path("rpm/post.spec")
lines = spec_path.read_text(encoding="utf-8").splitlines(True)

out: list[str] = []
requires_written = False
skip_requires = False
for line in lines:
    if skip_requires:
        if line.startswith("Requires:"):
            continue
        skip_requires = False

    if line.startswith("Requires:") and not requires_written:
        for req in dnf_requires:
            out.append(f"Requires:       {req}\n")
        requires_written = True
        skip_requires = True
        continue

    if line.startswith("Requires:") and requires_written:
        # Drop any leftover Requires lines from a previous longer list.
        continue

    out.append(line)

if not requires_written:
    raise SystemExit("ERROR: Could not find 'Requires:' line in rpm/post.spec")

spec_path.write_text("".join(out), encoding="utf-8")
print("Updated rpm/post.spec Requires from [tool.rpm].dnf_requires")
PY

TOPDIR="$REPO_ROOT/.rpmbuild"
rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# Working-tree tarball (includes Requires sync); exclude build/VCS noise.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/post-${VERSION}"
tar -C "$REPO_ROOT" \
  --exclude='.git' \
  --exclude='.rpmbuild' \
  --exclude='dist' \
  --exclude='.venv' \
  --exclude='build' \
  --exclude='debian/post' \
  --exclude='debian/.debhelper' \
  --exclude='debian/.build-venv' \
  --exclude='debian/files' \
  --exclude='debian/*.log' \
  --exclude='debian/*.substvars' \
  --exclude='debian/debhelper-build-stamp' \
  --exclude='__pycache__' \
  --exclude='*.egg-info' \
  --exclude='.pytest_cache' \
  -cf - . | tar -C "$STAGE/post-${VERSION}" -xf -

tar -C "$STAGE" -czf "$TOPDIR/SOURCES/post-${VERSION}.tar.gz" "post-${VERSION}"
cp "$REPO_ROOT/rpm/post.spec" "$TOPDIR/SPECS/post.spec"

rpmbuild -bb \
  --define "_topdir $TOPDIR" \
  --define "_build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm" \
  "$TOPDIR/SPECS/post.spec"

RPM="post-${VERSION}-1.noarch.rpm"
BUILT="$(find "$TOPDIR/RPMS" -type f -name "$RPM" -print -quit || true)"
if [ -z "$BUILT" ]; then
  # Some rpmbuild setups still inject dist tags; accept the noarch artifact.
  BUILT="$(find "$TOPDIR/RPMS" -type f -name "post-${VERSION}-1*.noarch.rpm" -print -quit || true)"
fi
if [ -z "$BUILT" ]; then
  echo "ERROR: expected noarch RPM for post-${VERSION}-1 after build" >&2
  find "$TOPDIR/RPMS" -type f -name '*.rpm' -print >&2 || true
  exit 1
fi

rm -rf dist
mkdir -p dist
cp -f "$BUILT" "dist/$RPM"

echo "Wrote dist/$RPM"
