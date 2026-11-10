#!/usr/bin/env bash
# Runs ON the remote host (launched detached by run.sh).
# Usage: remote_run.sh <lang> <run_id>
# Reloads the table, runs the benchmark, writes:
#   results/<run_id>.status  RUNNING | DONE | FAILED
#   results/<run_id>.json    result line (on success)
#   results/<run_id>.log     diagnostics
set -u
cd "$(dirname "$0")/.."
LANG_IMPL=$1
RUN_ID=$2
mkdir -p results
STATUS="results/$RUN_ID.status"
echo RUNNING > "$STATUS"

if (
    set -euo pipefail
    bash harness/remote_prepare.sh
    set -a
    source config/mysql.env
    source common/workload.env
    set +a
    bash "impl/$LANG_IMPL/run.sh" > "results/$RUN_ID.json"
) > "results/$RUN_ID.log" 2>&1; then
    echo DONE > "$STATUS"
else
    echo FAILED > "$STATUS"
fi
