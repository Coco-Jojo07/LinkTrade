#!/usr/bin/env sh
# Runs every automated check of the project. Exit code is non-zero on failure.
set -e
cd "$(dirname "$0")/.."

echo "== Bridge unit tests"
if python3 -c "import pytest" 2>/dev/null; then
  python3 -m pytest -q bridge
else
  python3 bridge/test_bridge.py
fi

echo "== JavaScript syntax"
for f in extension/*.js tests/*.js; do
  node --check "$f"
  echo "ok $f"
done

echo "== Manifest and static checks"
python3 tests/check_extension.py

if [ "${SKIP_E2E:-0}" != "1" ]; then
  echo "== Extension end-to-end (Chromium + Playwright)"
  NODE_PATH="${NODE_PATH:-$(npm root -g)}" node tests/extension_e2e.js
fi
