#!/usr/bin/env bash
# Runs ON a benchmark host: quick correctness smoke of one implementation
# against the current table state (no reload). Usage: remote_smoke.sh <lang> [threads] [txns]
set -euo pipefail
cd "$(dirname "$0")/.."
LANG_IMPL=$1
THREADS=${2:-4}
TXNS=${3:-25}
set -a
source config/mysql.env
source common/workload.env
set +a
export BENCH_THREADS="$THREADS" BENCH_WARMUP_TXNS=2 BENCH_TXNS_PER_THREAD="$TXNS"
exec bash "impl/$LANG_IMPL/run.sh"
