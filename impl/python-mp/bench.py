#!/usr/bin/env python3
"""MySQL language benchmark - Python, multi-process variant (mysqlclient).

A separate benchmark from impl/python: same four SPEC v3 workloads, same
driver (mysqlclient / MySQLdb), same per-worker counts, PRNG, barrier timing
and JSON output, but written the way a performance-minded Python application
would be, and reported as lang "python-mp" so its results never mix with the
canonical Python results.

What differs from impl/python, and why:

1. Workers are forked processes, not threads (like the PHP and Lua
   implementations). Each process has its own interpreter and GIL, so workers
   no longer take turns holding one global lock. Processes are forked after
   the driver is imported; every child opens its own connection.
2. Low-level driver API. Workers call the C-level `connection.query()` /
   `store_result()` / `fetch_row()` of mysqlclient directly instead of going
   through the pure-Python cursor layer (`cursor.execute`, parameter
   interpolation, row-by-row fetch).
3. Blob inserts send the blob as a hex literal (X'...'). mysqlclient's
   escape_string costs about 27 ms of CPU per MiB; bytes.hex() costs about
   4 ms. The server stores identical bytes either way.
4. Integer reads sum the fetched column with C-level builtins
   (sum(map(itemgetter(0), rows))) instead of a Python for loop.
5. The cyclic garbage collector is disabled in workers. The workloads create
   no reference cycles, and with 100,000 row tuples per fetch the collector
   would otherwise run repeatedly over objects that can never be garbage.

Unchanged: no server-side prepared statements (mysqlclient has none), so the
result reports prepared=false, as impl/python does.

Timing: the parent forks BENCH_THREADS children; each connects and prepares,
then waits at a multiprocessing.Barrier shared with the parent. When it
trips, the parent samples t_start and then sets a `go` Event that the children
wait on, so no child starts work before t_start. t_end is sampled after every
child has exited. Each child writes its latency samples to a private temp file after
its work and before exiting; the parent reads the files after t_end.

Progress: each child publishes its completed-op count to its own slot in a
shared-memory array; the parent sums the slots every BENCH_PROGRESS_SEC
seconds while waiting for children and atomically rewrites the progress file.
"""

import array
import gc
import hashlib
import json
import multiprocessing as mp
import os
import platform
import random
import socket
import struct
import sys
import tempfile
import time
import traceback
from operator import itemgetter

import MySQLdb

LANG = "python-mp"

LCG_MUL = 1103515245
LCG_INC = 12345
LCG_MASK = 0x7FFFFFFF  # 2^31 - 1; validation (seed 42, worker 0): 1250496027

WORKLOAD_META = {
    1: ("int-insert", "rows/s"),
    2: ("int-read", "rows/s"),
    3: ("blob-insert", "blobs/s"),
    4: ("blob-read", "blobs/s"),
}

# Seconds the parent waits at the start barrier for all children to connect.
BARRIER_TIMEOUT_S = 900


def getenv_int(name, default=None):
    val = os.environ.get(name)
    if val is None or val == "":
        if default is not None:
            return default
        print(f"missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return int(val)


# ---- Workloads (run inside a child; return latencies as array('q')) --------

def wl1_int_insert(conn, index, cfg, done):
    batch = cfg["int_batch"]
    execs = cfg["int_rows"] // batch
    # One statement template; values are fresh LCG outputs in [0, 2^31-1],
    # which fit a signed 32-bit INT as is.
    template = "INSERT INTO bench_int (val) VALUES " + ",".join(["(%d)"] * batch)
    state = cfg["seed"] + index
    query = conn.query
    clock = time.monotonic_ns
    lat = array.array("q")
    append = lat.append
    vals = [0] * batch
    rng = range(batch)
    for n in range(1, execs + 1):
        for j in rng:
            state = (state * LCG_MUL + LCG_INC) & LCG_MASK
            vals[j] = state
        sql = template % tuple(vals)
        t0 = clock()
        query(sql)
        append((clock() - t0) // 1000)
        done[index] = n * batch
    return lat, 0


def wl2_int_read(conn, index, cfg, done):
    sql = f"SELECT val FROM bench_int LIMIT {cfg['int_rows']}"
    lat = array.array("q")
    t0 = time.monotonic_ns()
    conn.query(sql)
    rows = conn.store_result().fetch_row(0)  # all rows, C-level conversion
    total = sum(map(itemgetter(0), rows))    # 64-bit sum (Python int)
    lat.append((time.monotonic_ns() - t0) // 1000)
    done[index] = len(rows)
    return lat, total


def wl3_blob_insert(conn, index, cfg, done):
    count = cfg["blob_count"]
    size = cfg["blob_size"]
    # Buffer filled once per worker with the native randbytes (SPEC WL3);
    # the first 8 bytes are overwritten with the blob id before each blob.
    buf = bytearray(random.Random(cfg["seed"] + index).randbytes(size))
    query = conn.query
    md5 = hashlib.md5
    clock = time.monotonic_ns
    lat = array.array("q")
    append = lat.append
    sink = 0
    for k in range(count):
        blob_id = index * count + k
        struct.pack_into("<Q", buf, 0, blob_id)
        sink ^= md5(buf).digest()[0]
        t0 = clock()
        # Hex literal: same stored bytes, far cheaper than escape_string.
        query(b"".join((b"INSERT INTO bench_blob (id, data) VALUES (%d, X'" % blob_id,
                        buf.hex().encode("ascii"), b"')")))
        append((clock() - t0) // 1000)
        done[index] = k + 1
    return lat, sink


def wl4_blob_read(conn, index, cfg, done):
    count = cfg["blob_count"]
    query = conn.query
    store = conn.store_result
    md5 = hashlib.md5
    clock = time.monotonic_ns
    lat = array.array("q")
    append = lat.append
    sink = 0
    for k in range(count):
        blob_id = index * count + k
        t0 = clock()
        query(b"SELECT data FROM bench_blob WHERE id = %d" % blob_id)
        data = store().fetch_row()[0][0]
        append((clock() - t0) // 1000)
        sink ^= md5(data).digest()[0]
        done[index] = k + 1
    return lat, sink


WORKLOADS = {1: wl1_int_insert, 2: wl2_int_read, 3: wl3_blob_insert, 4: wl4_blob_read}


def child_main(index, workload, cfg, conn_args, barrier, go, done, out_path):
    """Runs in a forked child. Never returns: exits with os._exit."""
    code = 1
    try:
        gc.disable()
        conn = MySQLdb.connect(ssl_mode="DISABLED", **conn_args)
        conn.autocommit(True)
        fn = WORKLOADS[workload]
        barrier.wait()   # all children connected; parent samples t_start ...
        go.wait()        # ... and only then releases the children
        lat, sink = fn(conn, index, cfg, done)
        conn.close()
        # Latencies + sink to a private file; the parent reads it after t_end.
        with open(out_path, "wb") as f:
            f.write(struct.pack("<q", sink & 0x7FFFFFFFFFFFFFFF))
            lat.tofile(f)
        code = 0
    except BaseException:  # noqa: BLE001 - reported, then non-zero exit
        try:
            barrier.abort()
        except Exception:  # noqa: BLE001
            pass
        print(f"worker {index} error:\n{traceback.format_exc()}", file=sys.stderr)
    finally:
        sys.stderr.flush()
        os._exit(code)


def write_progress(path, done, t_start, meta, threads):
    try:
        ops = sum(done)
        elapsed = (time.monotonic_ns() - t_start) / 1e9
        obj = {
            "workload": meta[0], "workload_name": meta[1], "unit": meta[2],
            "threads": threads, "ops": ops, "elapsed_s": round(elapsed, 3),
            "throughput": round(ops / elapsed if elapsed > 0 else 0.0, 1),
            "partial": True,
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - progress is best-effort
        pass


def driver_version_string():
    try:
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
    cfg = {
        "seed": getenv_int("BENCH_SEED"),
        "int_rows": getenv_int("BENCH_INT_ROWS", default=1000000),
        "int_batch": getenv_int("BENCH_INT_BATCH", default=10),
        "blob_count": getenv_int("BENCH_BLOB_COUNT", default=100),
        "blob_size": getenv_int("BENCH_BLOB_SIZE", default=1048576),
    }
    progress_file = os.environ.get("BENCH_PROGRESS_FILE")
    progress_sec = getenv_int("BENCH_PROGRESS_SEC", default=5)
    workload_name, unit = WORKLOAD_META[workload]

    ctx = mp.get_context("fork")
    # Two-step start. A Barrier alone is not enough: after it trips, 513
    # processes contend to re-acquire its internal lock and the parent can be
    # among the last to wake, so children would start work before t_start.
    # Children wait on `go` after the barrier; the parent sets it only after
    # sampling t_start, so no work can precede the timed window.
    barrier = ctx.Barrier(threads + 1)
    go = ctx.Event()
    done = ctx.Array("q", threads, lock=False)  # one slot per child
    tmpdir = tempfile.mkdtemp(prefix="bench-pymp-",
                              dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
    paths = [os.path.join(tmpdir, f"w{i}.lat") for i in range(threads)]

    # Flush before forking so children don't inherit and re-emit buffered output.
    sys.stdout.flush()
    sys.stderr.flush()
    pids = []
    for i in range(threads):
        pid = os.fork()
        if pid == 0:
            child_main(i, workload, cfg, conn_args, barrier, go, done, paths[i])
        pids.append(pid)

    failed = False
    t_start = None
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT_S)
        t_start = time.monotonic_ns()
    except Exception:  # noqa: BLE001 - BrokenBarrierError or timeout
        failed = True
    go.set()  # release children (on failure they exit via the broken barrier)

    # Reap children; between reaps, write progress on schedule.
    remaining = set(pids)
    next_progress = time.monotonic() + progress_sec
    while remaining:
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == 0:
            if progress_file and t_start is not None and time.monotonic() >= next_progress:
                write_progress(progress_file, done, t_start,
                               (workload, workload_name, unit), threads)
                next_progress += progress_sec
            time.sleep(0.002)
            continue
        if pid in remaining:
            remaining.discard(pid)
            if os.waitstatus_to_exitcode(status) != 0:
                failed = True
    t_end = time.monotonic_ns()

    try:
        if failed or t_start is None:
            print("one or more workers failed", file=sys.stderr)
            sys.exit(1)
        elapsed_s = (t_end - t_start) / 1e9
        latencies = array.array("q")
        sink = 0
        for p in paths:
            with open(p, "rb") as f:
                sink ^= struct.unpack("<q", f.read(8))[0]
                latencies.frombytes(f.read())
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    lat = sorted(latencies)
    n = len(lat)
    if n == 0:
        print("no latency samples recorded", file=sys.stderr)
        sys.exit(1)

    def pctile(x):
        return lat[-(-(x * n) // 100) - 1]

    if workload in (1, 2):
        ops = threads * cfg["int_rows"]
        mb_per_s = 0
    else:
        ops = threads * cfg["blob_count"]
        mb_per_s = round(ops * cfg["blob_size"] / 1e6 / elapsed_s, 1)

    result = {
        "lang": LANG,
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
        "throughput": round(ops / elapsed_s, 1),
        "mb_per_s": mb_per_s,
        "p50_us": pctile(50),
        "p95_us": pctile(95),
        "p99_us": pctile(99),
        "max_us": lat[n - 1],
        "client_cpu_s": 0,
        "server_cpu_s": 0,
    }
    print(json.dumps(result, separators=(",", ":")))
    if sink == -1:  # never true; keeps the sink observably used
        print("", file=sys.stderr)

    if progress_file:
        for path in (progress_file, progress_file + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
