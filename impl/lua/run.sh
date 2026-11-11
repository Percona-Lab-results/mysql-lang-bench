#!/usr/bin/env bash
# Run the Lua benchmark. Config comes from environment variables (see SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec luajit "$SCRIPT_DIR/bench.lua"
