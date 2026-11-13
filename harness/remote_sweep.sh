#!/usr/bin/env bash
# Runs ON a benchmark host (launched detached by run_sweep.sh).
# Usage: remote_sweep.sh <sweep_id> <lang> [lang...]
#
# v3 micro-workloads. For each language: create the schema, then for each
# worker count run the configured workloads in order (1 insert ints, 2 read
# ints, 3 insert blobs, 4 read blobs), truncating before each insert workload
# so reads see freshly-inserted (cached) data. No pre-warming. Go and C# are
# run twice per cell -- stock and tuned runtime -- tagged in the JSON.
#
# Per-run artifact: results/<sweep_id>-<lang>-w<W>-t<N>[-stock|-tuned].json
# Status: results/<sweep_id>.status ; log: results/<sweep_id>.log
set -u
cd "$(dirname "$0")/.."
SWEEP_ID=$1; shift
LANGS="$*"
mkdir -p results
STATUS="results/$SWEEP_ID.status"
LOG="results/$SWEEP_ID.log"
# 512 forked workers (PHP/Lua) and many connections need generous fd limits.
ulimit -n 65536 2>/dev/null || true

set -a; source config/mysql.env; source "${BENCH_WORKLOAD_ENV:-common/workload.env}"; set +a
CLK=$(getconf CLK_TCK)
MYSQL=("${MYSQL_CLI:-mysql}" --no-defaults --protocol=TCP -h "$MYSQL_HOST" \
       -P "$MYSQL_PORT" -u "$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DB")

overall=DONE
for L in $LANGS; do
    echo "RUNNING $L schema" > "$STATUS"
    if ! "${MYSQL[@]}" < common/schema.sql >> "$LOG" 2>&1; then
        overall="FAILED ($L schema)"; break
    fi
    fail=0
    for N in $BENCH_THREADS_LIST; do
        for W in $BENCH_WORKLOADS; do
            # Go and C# run stock + tuned; everyone else runs once.
            case "$L" in
                go|csharp) VARIANTS="0 1" ;;
                *)         VARIANTS="-" ;;
            esac
            for V in $VARIANTS; do
                # Fresh table before EACH insert run (reads keep the data).
                # Must be per-variant: go/csharp run an insert cell twice, and
                # the blob table has a PRIMARY KEY, so a second un-truncated
                # run would hit duplicate-key on the same ids.
                if [ "$W" = 1 ]; then "${MYSQL[@]}" -e "TRUNCATE bench_int;"  >> "$LOG" 2>&1; fi
                if [ "$W" = 3 ]; then "${MYSQL[@]}" -e "TRUNCATE bench_blob;" >> "$LOG" 2>&1; fi
                suffix=""; tuned="null"
                if [ "$V" = 0 ]; then suffix="-stock"; tuned="false"; fi
                if [ "$V" = 1 ]; then suffix="-tuned"; tuned="true";  fi
                OUT="results/$SWEEP_ID-$L-w$W-t$N$suffix.json"
                CPUF="results/.cpu.$$"
                echo "RUNNING $L w=$W t=$N${suffix}" > "$STATUS"
                MYSQLD_PID=$(pgrep -x mysqld | head -1)
                SRV0=$(awk '{print $14+$15}' "/proc/$MYSQLD_PID/stat")
                W0=$(date +%s.%N)
                if ( set -euo pipefail
                     set -a; source config/mysql.env; source "${BENCH_WORKLOAD_ENV:-common/workload.env}"; set +a
                     export BENCH_WORKLOAD="$W" BENCH_THREADS="$N"
                     export BENCH_PROGRESS_FILE="$OUT.progress"
                     [ "$V" != "-" ] && export BENCH_TUNED="$V"
                     /usr/bin/time -o "$CPUF" -f "%U %S" bash "impl/$L/run.sh" > "$OUT.tmp"
                   ) >> "$LOG" 2>&1; then
                    W1=$(date +%s.%N)
                    SRV1=$(awk '{print $14+$15}' "/proc/$MYSQLD_PID/stat")
                    python3 - "$OUT" "$CPUF" "$SRV0" "$SRV1" "$CLK" "$W0" "$W1" "$tuned" <<'PYEOF'
import json, sys
out, cpuf = sys.argv[1], sys.argv[2]
srv0, srv1, clk, w0, w1 = map(float, sys.argv[3:8])
tuned = sys.argv[8]
u, s = map(float, open(cpuf).read().split()[:2])
r = json.load(open(out + ".tmp"))
r["client_cpu_s"] = round(u + s, 2)
r["server_cpu_s"] = round((srv1 - srv0) / clk, 2)
r["run_wall_s"] = round(w1 - w0, 2)
if tuned != "null":
    r["tuned"] = (tuned == "true")
json.dump(r, open(out, "w"), separators=(",", ":"))
PYEOF
                    rm -f "$OUT.tmp" "$CPUF" "$OUT.progress"
                    echo "$L w=$W t=$N$suffix ok" >> "$LOG"
                else
                    # leave $OUT.progress behind: it holds the partial result of
                    # this interrupted/failed cell for recovery.
                    rm -f "$OUT.tmp" "$CPUF"
                    echo "$L w=$W t=$N$suffix FAILED" >> "$LOG"
                    overall="FAILED ($L w=$W t=$N$suffix)"; fail=1; break
                fi
            done
            [ "$fail" = 1 ] && break
        done
        [ "$fail" = 1 ] && break
    done
    [ "$fail" = 1 ] && break
done
echo "$overall" > "$STATUS"
