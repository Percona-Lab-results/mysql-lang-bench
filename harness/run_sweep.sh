#!/usr/bin/env bash
# Usage: run_sweep.sh <host> [lang...]
# Launches a detached sweep on the remote host covering the given languages
# (default: all languages mapped to the host); prints the sweep id.
source "$(dirname "$0")/lib.sh"
HOST=$1; shift
LANGS=${*:-$(langs_for_host "$HOST")}
SWEEP_ID="sweep-$(date +%Y%m%d-%H%M%S)-$(echo $LANGS | tr ' ' '+')"
# Fully detach the remote sweep so ssh returns immediately, letting run_all
# launch every host in parallel. Three things are required, and all were
# missing before (which serialized the hosts and ballooned wall time):
#   ssh -n .............. the local ssh reads stdin from /dev/null, so it does
#                         not hold the channel open waiting on our stdin;
#   nohup ... & ......... the remote job ignores the SIGHUP sent when the
#                         channel closes, so it survives after ssh returns;
#   </dev/null >/dev/null 2>&1 .. the backgrounded job shares none of the
#                         channel's three std streams, so sshd can EOF and
#                         close the session instead of waiting for the job.
# The `&` must background ONLY the nohup, not the whole `cd && mkdir && nohup`
# list: with `cd && mkdir && nohup ... &`, bash backgrounds the entire AND-list
# as one job and the nohup runs in that job's foreground with the channel's
# stdout/stderr still attached, so ssh waits for the whole sweep (this is what
# serialized the hosts). Scoping the `&` inside a brace group fixes it.
# shellcheck disable=SC2086
ssh -n $SSH_OPTS "$SSH_USER@$HOST" \
    "cd $REMOTE_DIR && mkdir -p results && { nohup env BENCH_DURATION_OVERRIDE='${BENCH_DURATION_OVERRIDE:-}' bash harness/remote_sweep.sh $SWEEP_ID $LANGS </dev/null >/dev/null 2>&1 & }"
echo "$HOST $SWEEP_ID"
