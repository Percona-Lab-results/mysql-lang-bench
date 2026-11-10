#!/usr/bin/env bash
# Deploy + build + launch a full thread-count sweep on every host, poll until all
# sweeps finish, then collect results and print the report.
source "$(dirname "$0")/lib.sh"
POLL=${POLL:-30}

declare -A SWEEP_OF
for h in $(all_hosts); do
    langs=$(langs_for_host "$h")
    echo "== $h ($langs) =="
    bash "$ROOT/harness/deploy.sh" "$h"
    ok=1
    for lang in $langs; do
        bash "$ROOT/harness/build.sh" "$h" "$lang" || { echo "build failed: $lang on $h" >&2; ok=0; }
    done
    [ "$ok" = 1 ] || { echo "skipping $h" >&2; continue; }
    out=$(bash "$ROOT/harness/run_sweep.sh" "$h" $langs)
    SWEEP_OF[$h]=$(echo "$out" | awk 'END {print $2}')
    echo "started ${SWEEP_OF[$h]} on $h"
done

echo "== polling every ${POLL}s =="
while :; do
    pending=0
    for h in "${!SWEEP_OF[@]}"; do
        st=$(rssh "$h" "cat $REMOTE_DIR/results/${SWEEP_OF[$h]}.status 2>/dev/null" || echo UNKNOWN)
        printf '%-14s %-32s %s\n' "$h" "${SWEEP_OF[$h]}" "$st"
        case "$st" in RUNNING*|UNKNOWN) pending=1 ;; esac
    done
    [ "$pending" -eq 0 ] && break
    sleep "$POLL"
    echo "--"
done

bash "$ROOT/harness/collect.sh"
python3 "$ROOT/report/generate_report.py"
