#!/usr/bin/env bash
# Run the TypeScript benchmark. Config comes from environment variables (SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[ -f dist/bench.js ] || {
    echo "error: dist/bench.js not found — run 'bash impl/typescript/build.sh' first" >&2
    exit 1
}

exec node dist/bench.js
