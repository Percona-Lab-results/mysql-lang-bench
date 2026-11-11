#!/usr/bin/env bash
# Runs ON the remote host (launched detached by run_sweep.sh).
# Usage: remote_sweep.sh <sweep_id> <lang> [lang...]
# For each language: reload the table ONCE (full load is expensive at 10M rows),
# then run every thread count in BENCH_THREADS_LIST without reloading between
# rungs. Results: results/<sweep_id>-<lang>-t<N>.json. Status: results/<sweep_id>.status.
set -u
cd "$(dirname "$0")/.."
SWEEP_ID=$1; shift
LANGS="$*"
mkdir -p results
# 512 workers need >512 fds in one process (Node) / one parent (PHP);
# module loading across hundreds of isolates spikes well beyond that.
ulimit -n 65536 2>/dev/null || ulimit -n 4096 2>/dev/null || true
STATUS="results/$SWEEP_ID.status"
LOG="results/$SWEEP_ID.log"
source common/workload.env

overall=DONE
for L in $LANGS; do
    echo "RUNNING $L prepare" > "$STATUS"
    if ! bash harness/remote_prepare.sh >> "$LOG" 2>&1; then
        overall="FAILED ($L prepare)"
        break
    fi
    # One long READ-ONLY sysbench warm-up per sweep (both modes), after the
    # load and before rung 1, to initialize the server caches. Read-only
    # keeps the table byte-identical for every language.
    if [ "${BENCH_SYSBENCH_WARMUP_S:-0}" -gt 0 ]; then
        echo "RUNNING $L sysbench-warmup (${BENCH_SYSBENCH_WARMUP_S}s)" > "$STATUS"
        if ! (
            set -euo pipefail
            set -a; source config/mysql.env; source common/workload.env; set +a
            sysbench common/sysbench_warmup.lua \
                --mysql-host="$MYSQL_HOST" --mysql-port="$MYSQL_PORT" \
                --mysql-user="$MYSQL_USER" --mysql-password="$MYSQL_PASSWORD" \
                --mysql-db="$MYSQL_DB" --threads=32 \
                --time="$BENCH_SYSBENCH_WARMUP_S" --report-interval=60 run
        ) >> "$LOG" 2>&1; then
            overall="FAILED ($L sysbench-warmup)"
            break
        fi
    fi
    for T in $BENCH_THREADS_LIST; do
        echo "RUNNING $L t=$T" > "$STATUS"
        if (
            set -euo pipefail
            set -a
            source config/mysql.env
            source common/workload.env
            set +a
            export BENCH_THREADS="$T"
            OUT="results/$SWEEP_ID-$L-t$T.json"
            CPUF="results/$SWEEP_ID-$L-t$T.cpu"
            # CPU accounting: client = user+sys of the whole benchmark process
            # tree (covers forked/threaded workers); server = mysqld's
            # cumulative utime+stime delta over the same window. The window is
            # the full run (connect + warmup + timed + teardown), slightly
            # wider than elapsed_s but identical in shape for every language.
            MYSQLD_PID=$(pgrep -x mysqld | head -1)
            CLK=$(getconf CLK_TCK)
            SRV0=$(awk '{print $14+$15}' "/proc/$MYSQLD_PID/stat")
            W0=$(date +%s.%N)
            /usr/bin/time -o "$CPUF" -f "%U %S" bash "impl/$L/run.sh" > "$OUT"
            W1=$(date +%s.%N)
            SRV1=$(awk '{print $14+$15}' "/proc/$MYSQLD_PID/stat")
            python3 - "$OUT" "$CPUF" "$SRV0" "$SRV1" "$CLK" "$W0" "$W1" <<'PYEOF'
import json, sys
out, cpuf = sys.argv[1], sys.argv[2]
srv0, srv1, clk, w0, w1 = map(float, sys.argv[3:8])
u, s = map(float, open(cpuf).read().split()[:2])
r = json.load(open(out))
r["client_cpu_s"] = round(u + s, 2)
r["server_cpu_s"] = round((srv1 - srv0) / clk, 2)
r["run_wall_s"] = round(w1 - w0, 2)
json.dump(r, open(out, "w"), separators=(",", ":"))
PYEOF
            rm -f "$CPUF"
        ) >> "$LOG" 2>&1; then
            echo "$L t=$T ok" >> "$LOG"
        else
            echo "$L t=$T FAILED" >> "$LOG"
            overall="FAILED ($L t=$T)"
            break 2
        fi
    done
done
echo "$overall" > "$STATUS"
