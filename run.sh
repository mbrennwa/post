#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv --system-site-packages
  .venv/bin/pip install -e .
fi

exec .venv/bin/post "$@"
