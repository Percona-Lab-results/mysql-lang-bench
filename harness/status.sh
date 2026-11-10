#!/usr/bin/env bash
# Usage: status.sh [host]   — show the latest run status per host (all hosts if omitted).
source "$(dirname "$0")/lib.sh"
HOSTS=${1:-$(all_hosts)}
for h in $HOSTS; do
    st=$(rssh "$h" "cd $REMOTE_DIR/results 2>/dev/null || exit 0
        f=\$(ls -t *.status 2>/dev/null | head -1)
        [ -n \"\$f\" ] && echo \"\${f%.status} \$(cat \"\$f\")\"" 2>/dev/null) || st="(unreachable)"
    printf '%-14s %s\n' "$h" "${st:-'(no runs)'}"
done
