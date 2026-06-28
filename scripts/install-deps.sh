#!/usr/bin/env bash
# Install system packages declared in pyproject.toml [tool.deb].apt_depends.
set -euo pipefail
cd "$(dirname "$0")/.."

extra=()
while (($# > 0)); do
  case "$1" in
    --)
      shift
      extra+=("$@")
      break
      ;;
    *)
      extra+=("$1")
      shift
      ;;
  esac
done

mapfile -t apt_depends < <(python3 - <<'PY'
from pathlib import Path
import re
import tomllib

cfg = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
apt_depends = cfg.get("tool", {}).get("deb", {}).get("apt_depends", [])
if not apt_depends:
    raise SystemExit("ERROR: [tool.deb].apt_depends is missing or empty in pyproject.toml")


def apt_install_name(spec: str) -> str:
    """Strip Debian version constraints for apt-get (e.g. 'python3 (>= 3.10)')."""
    name = spec.strip().split()[0]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+.-]*", name):
        raise SystemExit(f"ERROR: invalid apt dependency: {spec!r}")
    return name


for pkg in apt_depends:
    print(apt_install_name(pkg))
PY
)

if ! command -v apt-get >/dev/null; then
  echo "post: apt-get not found — install these packages with your system package manager:" >&2
  printf '  %s\n' "${apt_depends[@]}"
  exit 1
fi

sudo apt-get update
sudo apt-get install -y "${apt_depends[@]}" "${extra[@]}"
