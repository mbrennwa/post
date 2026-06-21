#!/usr/bin/env bash
# Run each test module in its own process so Camel/EDS memory is released between modules.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
fi

if [[ "${SKIP_PIP_INSTALL:-}" != 1 ]]; then
  "$PYTHON" -m pip install -q -e ".[dev]"
fi

failed=0
for test_file in tests/test_*.py; do
  module=$(basename "$test_file")
  echo "==> ${module}"
  if ! "$PYTHON" -m unittest discover -s tests -p "$module" -q; then
    failed=1
  fi
done

if (( failed != 0 )); then
  exit 1
fi

"$PYTHON" -m reuse lint
