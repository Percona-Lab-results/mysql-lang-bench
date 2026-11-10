#!/usr/bin/env bash
# Run the JavaScript benchmark. Config comes from environment variables (SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec node bench.mjs
