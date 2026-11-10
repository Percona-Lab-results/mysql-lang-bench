#!/usr/bin/env bash
# Usage: collect.sh [host]   — rsync results back into results/<host>/ locally.
source "$(dirname "$0")/lib.sh"
HOSTS=${1:-$(all_hosts)}
for h in $HOSTS; do
    mkdir -p "$ROOT/results/$h"
    # shellcheck disable=SC2086
    rsync -az -e "ssh $SSH_OPTS" "$SSH_USER@$h:$REMOTE_DIR/results/" "$ROOT/results/$h/" \
        && echo "collected $h" || echo "collect FAILED for $h" >&2
done
