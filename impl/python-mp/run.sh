#!/usr/bin/env bash
# Run the multi-process Python benchmark (lang "python-mp"). Config comes from
# environment variables (SPEC.md). Uses impl/python's venv when present.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/../python/venv/bin/python"
if [ -x "$VENV_PY" ]; then PY="$VENV_PY"; else PY=python3; fi
exec "$PY" "$SCRIPT_DIR/bench.py"
