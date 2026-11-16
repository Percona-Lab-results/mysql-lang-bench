#!/usr/bin/env bash
# Run the Go benchmark. Invoked from the repo root (bash impl/go/run.sh);
# config comes from environment variables only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$DIR/bench" ]; then
    echo "benchmark binary not found: run 'bash impl/go/build.sh' first" >&2
    exit 1
fi

# Stock Go runtime defaults (the tuned GOMAXPROCS/GOGC variant was removed).
exec "$DIR/bench"
