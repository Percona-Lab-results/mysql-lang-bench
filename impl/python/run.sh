#!/usr/bin/env bash
# Run the Python benchmark. Config comes from environment variables (SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    PY="$SCRIPT_DIR/venv/bin/python"
else
    PY=python3
fi

exec "$PY" "$SCRIPT_DIR/bench.py"
