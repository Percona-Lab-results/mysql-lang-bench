#!/usr/bin/env bash
# Run the Go benchmark. Invoked from the repo root (bash impl/go/run.sh);
# config comes from environment variables only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -x "$DIR/bench" ]; then
    echo "benchmark binary not found: run 'bash impl/go/build.sh' first" >&2
    exit 1
fi

# The harness runs Go twice per cell: a stock variant (BENCH_TUNED=0 or unset)
# that keeps the Go runtime defaults, and a tuned variant (BENCH_TUNED=1). The
# workers are mostly blocked on network I/O, so on a many-core host fanning GC
# and the scheduler across every P thrashes; the tuned variant caps parallelism
# and collects far less often.
if [ "${BENCH_TUNED:-0}" = "1" ]; then
    export GOMAXPROCS=16
    export GOGC=800
fi

exec "$DIR/bench"
