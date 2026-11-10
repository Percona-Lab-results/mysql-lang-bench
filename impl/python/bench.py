#!/usr/bin/env python3
"""MySQL language benchmark - Python implementation (mysqlclient / MySQLdb).

Follows SPEC.md v2 exactly: multithreaded sysbench-oltp_read_write-style
transactional workload. One threading.Thread per worker, each with its own
connection (autocommit ON, explicit BEGIN/COMMIT as plain statements), its own
31-bit LCG PRNG seeded BENCH_SEED + worker_index, barrier-based timing,
retry on MySQL errors 1213/1205/1062 with full value redraw, per-transaction
latency in integer microseconds, one JSON result line on stdout.

mysqlclient releases the GIL during network I/O; GIL contention at high thread
counts is authentic Python behaviour (SPEC.md: do not use multiprocessing).
"""

import json
import os
import platform
import socket
import sys
import threading
import time

import MySQLdb

C_VAL = "abcdefghij" * 12   # 120 chars
PAD_VAL = "0123456789" * 6  # 60 chars

RETRYABLE = (1213, 1205, 1062)  # deadlock, lock wait timeout, duplicate key

SQL_POINT = "SELECT c FROM sbtest WHERE id = %s"
SQL_RANGE = "SELECT c FROM sbtest WHERE id BETWEEN %s AND %s"
SQL_SUM = "SELECT SUM(k) FROM sbtest WHERE id BETWEEN %s AND %s"
SQL_ORDER = "SELECT c FROM sbtest WHERE id BETWEEN %s AND %s ORDER BY c"
SQL_DISTINCT = "SELECT DISTINCT c FROM sbtest WHERE id BETWEEN %s AND %s ORDER BY c"
SQL_IDX_UPDATE = "UPDATE sbtest SET k = k + 1 WHERE id = %s"
SQL_NONIDX_UPDATE = "UPDATE sbtest SET c = %s WHERE id = %s"
SQL_DELETE = "DELETE FROM sbtest WHERE id = %s"
SQL_INSERT = "INSERT INTO sbtest (id, k, c, pad) VALUES (%s, %s, %s, %s)"


def getenv_int(name):
    val = os.environ.get(name)
    if val is None:
        print(f"missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return int(val)


class Worker(threading.Thread):
    def __init__(self, index, conn_args, table_rows, range_size,
                 warmup_txns, txns, seed, barrier):
        super().__init__(name=f"worker-{index}")
        self.index = index
        self.conn_args = conn_args
        self.table_rows = table_rows
        self.range_size = range_size
        self.warmup_txns = warmup_txns
        self.txns = txns
        self.seed = seed
        self.barrier = barrier
        self.latencies = [0] * txns  # pre-allocated (integer microseconds)
        self.retries = 0
        self.error = None

    def run(self):
        try:
            self.work()
        except BaseException as e:  # noqa: BLE001 - reported by main, exit 1
            self.error = e
            # Never leave main (or siblings) stuck at the start barrier.
            self.barrier.abort()

    def work(self):
        # Own PRNG stream: 31-bit LCG, state seeded BENCH_SEED + worker index.
        state = self.seed + self.index

        def rand(n):
            nonlocal state
            state = (state * 1103515245 + 12345) % 2147483648
            return state % n

        n_rows = self.table_rows
        r_size = self.range_size

        # ssl_mode DISABLED: libmysqlclient defaults to PREFERRED, which would
        # put this driver on TLS while other languages run plaintext.
        conn = MySQLdb.connect(ssl_mode="DISABLED", **self.conn_args)
        try:
            conn.autocommit(True)  # MySQLdb defaults autocommit OFF
            cur = conn.cursor()

            def attempt():
                # One transaction attempt; values drawn immediately before
                # each statement (draw order = execution order).
                cur.execute("BEGIN")
                for _ in range(10):
                    cur.execute(SQL_POINT, (1 + rand(n_rows),))
                    cur.fetchall()
                lo = 1 + rand(n_rows)
                cur.execute(SQL_RANGE, (lo, lo + r_size - 1))
                cur.fetchall()
                lo = 1 + rand(n_rows)
                cur.execute(SQL_SUM, (lo, lo + r_size - 1))
                cur.fetchone()
                lo = 1 + rand(n_rows)
                cur.execute(SQL_ORDER, (lo, lo + r_size - 1))
                cur.fetchall()
                lo = 1 + rand(n_rows)
                cur.execute(SQL_DISTINCT, (lo, lo + r_size - 1))
                cur.fetchall()
                cur.execute(SQL_IDX_UPDATE, (1 + rand(n_rows),))
                cur.execute(SQL_NONIDX_UPDATE, (C_VAL, 1 + rand(n_rows)))
                row_id = 1 + rand(n_rows)
                k = rand(n_rows)
                cur.execute(SQL_DELETE, (row_id,))
                cur.execute(SQL_INSERT, (row_id, k, C_VAL, PAD_VAL))
                cur.execute("COMMIT")

            def txn():
                # Re-run (with freshly drawn values) until it commits.
                while True:
                    try:
                        attempt()
                        return
                    except MySQLdb.Error as e:
                        code = e.args[0] if e.args else None
                        if code not in RETRYABLE:
                            raise
                        try:
                            cur.execute("ROLLBACK")
                        except MySQLdb.Error:
                            pass  # ignore rollback errors
                        self.retries += 1

            for _ in range(self.warmup_txns):
                txn()

            self.retries = 0  # report retries from the timed window only
            self.barrier.wait()

            for j in range(self.txns):
                t0 = time.monotonic_ns()
                txn()
                self.latencies[j] = (time.monotonic_ns() - t0) // 1000

            cur.close()
        finally:
            conn.close()


def main():
    conn_args = {
        "host": os.environ["MYSQL_HOST"],
        "port": getenv_int("MYSQL_PORT"),
        "user": os.environ["MYSQL_USER"],
        "passwd": os.environ["MYSQL_PASSWORD"],
        "db": os.environ["MYSQL_DB"],
    }

    table_rows = getenv_int("BENCH_TABLE_ROWS")
    threads = getenv_int("BENCH_THREADS")
    warmup_txns = getenv_int("BENCH_WARMUP_TXNS")
    txns_per_thread = getenv_int("BENCH_TXNS_PER_THREAD")
    range_size = getenv_int("BENCH_RANGE_SIZE")
    seed = getenv_int("BENCH_SEED")

    barrier = threading.Barrier(threads + 1)
    workers = [Worker(i, conn_args, table_rows, range_size,
                      warmup_txns, txns_per_thread, seed, barrier)
               for i in range(threads)]
    for w in workers:
        w.start()

    broken = False
    try:
        barrier.wait()  # all workers connected and warmed up
        t_start = time.monotonic_ns()
    except threading.BrokenBarrierError:
        broken = True

    for w in workers:
        w.join()
    t_end = time.monotonic_ns()

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

    def pctile(x):
        # p(x) = arr[ceil(x/100 * n) - 1], 0-based
        idx = -(-(x * n) // 100) - 1  # ceil via negated floor div
        return latencies[idx]

    try:  # mysqlclient >= 2.3 removed MySQLdb.__version__
        driver_version = str(MySQLdb.__version__)
    except AttributeError:
        from importlib.metadata import version
        driver_version = version("mysqlclient")
    try:
        driver_version = f"{driver_version} (libmysqlclient {MySQLdb.get_client_info()})"
    except Exception:
        pass

    txns = threads * txns_per_thread
    tps = txns / elapsed_s
    result = {
        "lang": "python",
        "driver": "mysqlclient",
        "driver_version": driver_version,
        "runtime_version": platform.python_version(),
        "prepared": False,
        "client_host": socket.gethostname(),
        "db_host": conn_args["host"],
        "threads": threads,
        "txns": txns,
        "retries": sum(w.retries for w in workers),
        "errors": 0,
        "elapsed_s": round(elapsed_s, 3),
        "tps": round(tps, 1),
        "qps": round(tps * 20, 1),
        "p50_us": pctile(50),
        "p95_us": pctile(95),
        "p99_us": pctile(99),
        "max_us": latencies[n - 1],
    }
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
