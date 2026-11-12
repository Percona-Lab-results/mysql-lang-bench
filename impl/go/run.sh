#!/usr/bin/env bash
# Run the Go benchmark. Invoked from the repo root (bash impl/go/run.sh);
# config comes from environment variables only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$DIR/bench" ]; then
    echo "benchmark binary not found: run 'bash impl/go/build.sh' first" >&2
    exit 1
fi

# Runtime tuning for this client/DB benchmark on an 80-core host: the workers
# are mostly blocked on network I/O, so letting the scheduler fan GC and
# goroutine management across all 80 Ps thrashes. Cap parallelism and collect
# far less often. Measured ~3x throughput at 64+ workers vs stock defaults.
export GOMAXPROCS="${GOMAXPROCS:-16}"
export GOGC="${GOGC:-800}"

exec "$DIR/bench"
