#!/usr/bin/env bash
# Usage: run.sh <host> [lang]
# Launches one detached benchmark run on the remote host and prints its run id.
# Progress: harness/status.sh ; results: harness/collect.sh
source "$(dirname "$0")/lib.sh"
HOST=$1
LANG_IMPL=${2:-$(lang_for_host "$HOST")}
RUN_ID="${LANG_IMPL}-$(date +%Y%m%d-%H%M%S)"
rssh "$HOST" "cd $REMOTE_DIR && mkdir -p results && nohup bash harness/remote_run.sh $LANG_IMPL $RUN_ID >/dev/null 2>&1 & echo started"
echo "$HOST $RUN_ID"
