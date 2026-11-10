#!/usr/bin/env bash
# Run the Swift benchmark. Config comes from environment variables (see SPEC.md).
set -euo pipefail

cd "$(dirname "$0")"

BIN=.build/release/bench
if [[ ! -x "$BIN" ]]; then
    echo "error: swift benchmark binary not found ($PWD/$BIN) — run 'bash impl/swift/build.sh' first" >&2
    exit 1
fi

exec "$BIN"
