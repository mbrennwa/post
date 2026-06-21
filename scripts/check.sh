#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
fi

"$PYTHON" -m pip install -q -e ".[dev]"
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m reuse lint
