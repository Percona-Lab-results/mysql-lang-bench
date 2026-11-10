#!/usr/bin/env bash
# Usage: run_sweep.sh <host> [lang...]
# Launches a detached sweep on the remote host covering the given languages
# (default: all languages mapped to the host); prints the sweep id.
source "$(dirname "$0")/lib.sh"
HOST=$1; shift
LANGS=${*:-$(langs_for_host "$HOST")}
SWEEP_ID="sweep-$(date +%Y%m%d-%H%M%S)-$(echo $LANGS | tr ' ' '+')"
rssh "$HOST" "cd $REMOTE_DIR && mkdir -p results && nohup bash harness/remote_sweep.sh $SWEEP_ID $LANGS >/dev/null 2>&1 & echo started"
echo "$HOST $SWEEP_ID"
