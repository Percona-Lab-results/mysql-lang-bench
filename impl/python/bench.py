#!/usr/bin/env python3
"""MySQL language benchmark - Python implementation (mysqlclient / MySQLdb).

Follows SPEC.md v3 exactly: one of four micro-workloads selected by
BENCH_WORKLOAD (1..4), run by BENCH_THREADS concurrent OS threads. Each worker
owns its own MySQLdb connection (autocommit ON, TLS disabled via
ssl_mode=DISABLED), its own 31-bit LCG PRNG stream seeded BENCH_SEED + index,
and its own latency array (per-operation, integer microseconds). Workers
connect and prepare outside a start barrier; main releases the barrier, samples
a monotonic clock, joins, and prints one JSON result line on stdout.

mysqlclient formats parameters client-side (no server-side prepared
statements), so the result reports prepared=false. It releases the GIL during
network I/O; GIL contention at high thread counts is authentic Python
behaviour.

The four workloads (per-worker counts; nothing shared/divided across workers):
  1 int-insert : BENCH_INT_ROWS rows via one multi-row INSERT of BENCH_INT_BATCH
                 rows, executed BENCH_INT_ROWS/BENCH_INT_BATCH times.
  2 int-read   : SELECT val FROM bench_int LIMIT BENCH_INT_ROWS, sum in Python.
  3 blob-insert: BENCH_BLOB_COUNT blobs of BENCH_BLOB_SIZE PRNG bytes, MD5 each,
                 INSERT INTO bench_blob(id, data).
  4 blob-read  : read own BENCH_BLOB_COUNT blobs by id, MD5 each.

Intermediate progress (SPEC "Intermediate progress"): when BENCH_PROGRESS_FILE
is set, each worker publishes a plain-int `done` count to a slot it alone writes
(GIL-safe); a monitor thread sums the slots every BENCH_PROGRESS_SEC seconds and
atomically overwrites the progress file (write .tmp, os.replace). The file is
deleted on successful completion.
"""

import hashlib
import json
import os
import platform
import random
import socket
import struct
import sys
import threading
import time

import MySQLdb

# 31-bit LCG (SPEC "PRNG — per worker"): state = (state*MUL + INC) mod 2^31.
# mod 2^31 == & MASK (keep low 31 bits). Validation (seed 42, worker 0): first
# next() = 1250496027.
LCG_MUL = 1103515245
LCG_INC = 12345
LCG_MASK = 0x7FFFFFFF  # 2^31 - 1

# workload -> (workload_name, unit)
WORKLOAD_META = {
    1: ("int-insert", "rows/s"),
    2: ("int-read", "rows/s"),
    3: ("blob-insert", "blobs/s"),
    4: ("blob-read", "blobs/s"),
}

# Black-box sink: accumulate consumed values so the compiler/interpreter cannot
# treat the app-side work (sum, MD5 digests) as dead code.
_SINK = 0


def sink(value):
    global _SINK
    _SINK ^= value


def getenv_int(name, default=None):
    val = os.environ.get(name)
    if val is None or val == "":
        if default is not None:
            return default
        print(f"missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return int(val)


class Worker(threading.Thread):
    def __init__(self, index, conn_args, workload, cfg, barrier):
        super().__init__(name=f"worker-{index}")
        self.index = index
        self.conn_args = conn_args
        self.workload = workload
        self.cfg = cfg
        self.barrier = barrier
        self.latencies = []  # per-operation latencies, integer microseconds
        self.done = 0        # completed operations (rows WL1/2, blobs WL3/4)
        self.error = None

    def run(self):
        try:
            self.work()
        except BaseException as e:  # noqa: BLE001 - reported by main, exit 1
            self.error = e
            # Never leave main (or siblings) stuck at the start barrier.
            self.barrier.abort()

    def work(self):
        # Connection and statement prep happen outside the timed window.
        # ssl_mode DISABLED: libmysqlclient defaults to PREFERRED, which would
        # put this driver on TLS while other languages run plaintext.
        conn = MySQLdb.connect(ssl_mode="DISABLED", **self.conn_args)
        try:
            conn.autocommit(True)  # MySQLdb defaults autocommit OFF
            cur = conn.cursor()

            dispatch = {
                1: self._wl1_int_insert,
                2: self._wl2_int_read,
                3: self._wl3_blob_insert,
                4: self._wl4_blob_read,
            }[self.workload]

            self.barrier.wait()  # timed window starts after all workers arrive
            dispatch(cur)
            cur.close()
        finally:
            conn.close()

    # ---- Workload 1: integer insert -------------------------------------
    def _wl1_int_insert(self, cur):
        batch = self.cfg["int_batch"]
        execs = self.cfg["int_rows"] // batch
        # ONE multi-row INSERT string reused for every execute. mysqlclient
        # formats the parameters client-side (prepared=false). Each bound value
        # is a fresh next() cast to a signed 32-bit int; the LCG yields values
        # in [0, 2^31-1], all of which already fit a signed 32-bit INT.
        sql = "INSERT INTO bench_int (val) VALUES " + ",".join(["(%s)"] * batch)
        state = self.cfg["seed"] + self.index
        latencies = self.latencies
        append = latencies.append
        for _ in range(execs):
            params = [0] * batch
            for j in range(batch):
                state = (state * LCG_MUL + LCG_INC) & LCG_MASK
                params[j] = state
            t0 = time.monotonic_ns()
            cur.execute(sql, params)
            append((time.monotonic_ns() - t0) // 1000)
            self.done += batch

    # ---- Workload 2: integer fetch + sum --------------------------------
    def _wl2_int_read(self, cur):
        # LIMIT is a trusted config integer, embedded directly.
        sql = f"SELECT val FROM bench_int LIMIT {self.cfg['int_rows']}"
        t0 = time.monotonic_ns()
        cur.execute(sql)
        rows = cur.fetchall()
        total = 0  # 64-bit-wide accumulator (Python int is unbounded)
        for row in rows:
            total += row[0]
        self.latencies.append((time.monotonic_ns() - t0) // 1000)
        sink(total)
        self.done += len(rows)

    # ---- Workload 3: blob insert + MD5 ----------------------------------
    def _wl3_blob_insert(self, cur):
        count = self.cfg["blob_count"]
        size = self.cfg["blob_size"]
        sql = "INSERT INTO bench_blob (id, data) VALUES (%s, %s)"
        # Content generation must be cheap and comparable, NOT a per-byte
        # interpreted loop (SPEC Workload 3): fill the BENCH_BLOB_SIZE buffer
        # ONCE per worker with Python's fast native randbytes, then before each
        # blob overwrite its first 8 bytes with the blob id so every blob's
        # content (and MD5) differs. MD5 still hashes the whole buffer each time.
        buf = bytearray(random.Random(self.cfg["seed"] + self.index)
                        .randbytes(size))
        latencies = self.latencies
        append = latencies.append
        for k in range(count):
            blob_id = self.index * count + k
            struct.pack_into("<Q", buf, 0, blob_id)  # id into first 8 bytes
            data = bytes(buf)
            digest = hashlib.md5(data).digest()
            sink(digest[0])
            t0 = time.monotonic_ns()
            cur.execute(sql, (blob_id, data))
            append((time.monotonic_ns() - t0) // 1000)
            self.done += 1

    # ---- Workload 4: blob read + MD5 ------------------------------------
    def _wl4_blob_read(self, cur):
        count = self.cfg["blob_count"]
        sql = "SELECT data FROM bench_blob WHERE id = %s"
        latencies = self.latencies
        append = latencies.append
        for k in range(count):
            blob_id = self.index * count + k
            t0 = time.monotonic_ns()
            cur.execute(sql, (blob_id,))
            row = cur.fetchone()
            append((time.monotonic_ns() - t0) // 1000)
            digest = hashlib.md5(row[0]).digest()
            sink(digest[0])
            self.done += 1


class ProgressMonitor(threading.Thread):
    """Sums workers' `done` slots on a timer and atomically rewrites the
    progress file. Best-effort: write failures are ignored so progress
    checkpointing never aborts the benchmark."""

    def __init__(self, workers, t_start, path, interval, meta, threads):
        super().__init__(name="progress-monitor", daemon=True)
        self.workers = workers
        self.t_start = t_start
        self.path = path
        self.interval = interval
        self.workload, self.workload_name, self.unit = meta
        self.threads = threads
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.wait(self.interval):
            self._write()

    def _write(self):
        try:
            ops = sum(w.done for w in self.workers)
            elapsed = (time.monotonic_ns() - self.t_start) / 1e9
            throughput = ops / elapsed if elapsed > 0 else 0.0
            obj = {
                "workload": self.workload,
                "workload_name": self.workload_name,
                "unit": self.unit,
                "threads": self.threads,
                "ops": ops,
                "elapsed_s": round(elapsed, 3),
                "throughput": round(throughput, 1),
                "partial": True,
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(json.dumps(obj, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 - progress is best-effort
            pass


def driver_version_string():
    try:  # mysqlclient >= 2.3 removed MySQLdb.__version__
        version = str(MySQLdb.__version__)
    except AttributeError:
        from importlib.metadata import version as pkg_version
        version = pkg_version("mysqlclient")
    try:
        version = f"{version} (libmysqlclient {MySQLdb.get_client_info()})"
    except Exception:  # noqa: BLE001
        pass
    return version


def main():
    conn_args = {
        "host": os.environ["MYSQL_HOST"],
        "port": getenv_int("MYSQL_PORT"),
        "user": os.environ["MYSQL_USER"],
        "passwd": os.environ["MYSQL_PASSWORD"],
        "db": os.environ["MYSQL_DB"],
    }

    workload = getenv_int("BENCH_WORKLOAD")
    if workload not in WORKLOAD_META:
        print(f"BENCH_WORKLOAD must be 1..4, got {workload}", file=sys.stderr)
        sys.exit(1)
    threads = getenv_int("BENCH_THREADS")
    seed = getenv_int("BENCH_SEED")
    cfg = {
        "seed": seed,
        "int_rows": getenv_int("BENCH_INT_ROWS", default=1000000),
        "int_batch": getenv_int("BENCH_INT_BATCH", default=10),
        "blob_count": getenv_int("BENCH_BLOB_COUNT", default=100),
        "blob_size": getenv_int("BENCH_BLOB_SIZE", default=1048576),
    }
    progress_file = os.environ.get("BENCH_PROGRESS_FILE")
    progress_sec = getenv_int("BENCH_PROGRESS_SEC", default=5)

    workload_name, unit = WORKLOAD_META[workload]

    barrier = threading.Barrier(threads + 1)
    workers = [Worker(i, conn_args, workload, cfg, barrier)
               for i in range(threads)]
    for w in workers:
        w.start()

    monitor = None
    broken = False
    try:
        barrier.wait()  # all workers connected and prepared
        t_start = time.monotonic_ns()
        if progress_file:
            monitor = ProgressMonitor(
                workers, t_start, progress_file, progress_sec,
                (workload, workload_name, unit), threads)
            monitor.start()
    except threading.BrokenBarrierError:
        broken = True

    for w in workers:
        w.join()
    t_end = time.monotonic_ns()

    if monitor is not None:
        monitor.stop()
        monitor.join()

    failed = [w for w in workers if w.error is not None]
    if failed:
        for w in failed:
            if not isinstance(w.error, threading.BrokenBarrierError):
                print(f"worker {w.index} error: {w.error}", file=sys.stderr)
        sys.exit(1)
    if broken:
        print("start barrier broken without worker error", file=sys.stderr)
        sys.exit(1)

    elapsed_s = (t_end - t_start) / 1e9

    latencies = []
    for w in workers:
        latencies.extend(w.latencies)
    latencies.sort()
    n = len(latencies)
    if n == 0:
        print("no latency samples recorded", file=sys.stderr)
        sys.exit(1)

    def pctile(x):
        # p(x) = arr[ceil(x/100 * n) - 1], 0-based
        idx = -(-(x * n) // 100) - 1  # ceil via negated floor div
        return latencies[idx]

    if workload in (1, 2):
        ops = threads * cfg["int_rows"]
        mb_per_s = 0
    else:
        ops = threads * cfg["blob_count"]
        mb_per_s = round(ops * cfg["blob_size"] / 1e6 / elapsed_s, 1)

    throughput = round(ops / elapsed_s, 1)

    result = {
        "lang": "python",
        "workload": workload,
        "workload_name": workload_name,
        "unit": unit,
        "driver": "mysqlclient",
        "driver_version": driver_version_string(),
        "runtime_version": platform.python_version(),
        "prepared": False,
        "client_host": socket.gethostname(),
        "db_host": conn_args["host"],
        "threads": threads,
        "ops": ops,
        "errors": 0,
        "elapsed_s": round(elapsed_s, 3),
        "throughput": throughput,
        "mb_per_s": mb_per_s,
        "p50_us": pctile(50),
        "p95_us": pctile(95),
        "p99_us": pctile(99),
        "max_us": latencies[n - 1],
        "client_cpu_s": 0,
        "server_cpu_s": 0,
    }
    print(json.dumps(result, separators=(",", ":")))

    # Final JSON is on stdout; the partial progress file is now superseded.
    if progress_file:
        for path in (progress_file, progress_file + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
