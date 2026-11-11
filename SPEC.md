# Benchmark implementation contract (v2 — multithreaded oltp_read_write)

Every implementation under `impl/<lang>/` runs the same sysbench-`oltp_read_write`-style
transactional workload with `BENCH_THREADS` concurrent workers against one MySQL server,
and prints one JSON line. All implementations MUST follow this spec exactly.

## Files each implementation provides

- `impl/<lang>/build.sh` — builds / installs deps on the host it runs on. Idempotent;
  exits non-zero with a one-line hint when a dependency is missing.
- `impl/<lang>/run.sh` — executes the benchmark; config from env vars only; result JSON
  (one line) on stdout, diagnostics on stderr.

Both must work invoked from the repository root (`bash impl/<lang>/build.sh`).

## Input: environment variables

Connection (`config/mysql.env`): `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`,
`MYSQL_PASSWORD`, `MYSQL_DB`.

Workload (`common/workload.env`, all parsed as integers):

- `BENCH_TABLE_ROWS`      — rows pre-loaded (keys 1..N)
- `BENCH_THREADS`         — number of concurrent workers
- `BENCH_WARMUP_TXNS`     — per-worker transactions before timing starts
- `BENCH_TXNS_PER_THREAD` — per-worker timed transactions
- `BENCH_RANGE_SIZE`      — range query width (default 100)
- `BENCH_SEED`            — base PRNG seed

## Workers and connections

- A "worker" is an OS thread (C: pthreads, C++: std::thread, Rust: std::thread,
  Java: Thread, Python: threading.Thread) — except PHP, which has no threads and uses
  **forked processes** (pcntl_fork), one per worker.
- Each worker: its own connection, its own prepared statements, its own PRNG stream.
  No connection sharing, no pools.
- TCP to `MYSQL_HOST:MYSQL_PORT`; TLS disabled (see v1 rules: libmysqlclient-based
  drivers must set SSL_MODE_DISABLED + GET_SERVER_PUBLIC_KEY).
- Autocommit ON at the connection level; transactions are explicit `BEGIN`/`COMMIT`
  statements (plain queries, not prepared).

**Lifecycle with barriers** (critical for fair timing):

1. Main starts `BENCH_THREADS` workers. Each connects, prepares statements, runs
   `BENCH_WARMUP_TXNS` warmup transactions (untimed), then waits at a start barrier.
2. When all workers reach the barrier, main samples the monotonic clock (t_start) and
   releases them.
3. Each worker runs `BENCH_TXNS_PER_THREAD` timed transactions, recording per-txn
   latency, then finishes. Main joins all workers and samples t_end.
4. `elapsed_s = t_end - t_start`. Connection setup and warmup are never inside the
   timed window.

Python note: `mysqlclient` releases the GIL during network I/O, so threads overlap on
the wire; GIL contention at high thread counts is authentic Python behaviour — do not
work around it with multiprocessing.

PHP note: workers are forked children. Child k writes its latency array and counters to
`$TMPDIR` files; the parent implements the barrier with pipes (children signal ready,
parent signals start), then merges results. Only the parent prints JSON.

## PRNG — per worker

Same 31-bit LCG as v1. Worker `i` (0-based) seeds its own independent state with
`BENCH_SEED + i`:

```
state_i = BENCH_SEED + i
next(): state = (state * 1103515245 + 12345) mod 2^31 ; return state
rand(n): next() mod n
```

Validation (worker 0, seed 42): first raw `next()` values are
1250496027, 1116302264, 1000676753, 1668674806, 908095735 — `rand(100)` of those:
27, 64, 53, 6, 35.

## The transaction

Exactly sysbench `oltp_read_write` defaults. `N = BENCH_TABLE_ROWS`,
`R = BENCH_RANGE_SIZE`. Values are drawn immediately before executing each statement
(draw order = execution order). `?` = bound parameter; all 18 inner statements are
server-side prepared once per worker (Python: parameterized API, `prepared:false`).

```
BEGIN                                                  (plain query)
10 × point select:  id = 1+rand(N)
     SELECT c FROM sbtest WHERE id = ?
 1 × simple range:  lo = 1+rand(N)
     SELECT c FROM sbtest WHERE id BETWEEN ? AND ?          (lo, lo+R-1)
 1 × sum range:     lo = 1+rand(N)
     SELECT SUM(k) FROM sbtest WHERE id BETWEEN ? AND ?
 1 × order range:   lo = 1+rand(N)
     SELECT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c
 1 × distinct range: lo = 1+rand(N)
     SELECT DISTINCT c FROM sbtest WHERE id BETWEEN ? AND ? ORDER BY c
 1 × index update:  id = 1+rand(N)
     UPDATE sbtest SET k = k + 1 WHERE id = ?
 1 × non-index update: id = 1+rand(N)
     UPDATE sbtest SET c = ? WHERE id = ?                   (C_VAL, id)
 1 × delete+insert: id = 1+rand(N); k = rand(N)
     DELETE FROM sbtest WHERE id = ?
     INSERT INTO sbtest (id, k, c, pad) VALUES (?, ?, ?, ?) (id, k, C_VAL, PAD_VAL)
COMMIT                                                 (plain query)
```

- Every SELECT must fetch **all** returned rows.
- `C_VAL` = "abcdefghij" ×12 (120 chars), `PAD_VAL` = "0123456789" ×6 (60 chars).
- Ranges may extend past N (`BETWEEN` simply matches fewer rows) — do not clamp.

**Concurrency errors.** Under concurrency, deadlocks (MySQL error 1213), lock wait
timeouts (1205) and duplicate keys on the insert (1062) are expected. On any of these:
`ROLLBACK` (ignore rollback errors), increment the worker's `retries` counter, redraw
all values (PRNG stream continues) and re-run the whole transaction until it commits.
Any other error: print to stderr, exit non-zero.

## Timing and statistics

- Monotonic clocks only.
- Per-transaction latency in **integer microseconds** (truncated): from just before the
  first `BEGIN` of the transaction to after its successful `COMMIT` — retry time
  included. Pre-allocate each worker's latency array (`BENCH_TXNS_PER_THREAD`).
- After joining workers, merge all latency arrays, sort ascending,
  `p(x) = arr[ceil(x/100 * n) - 1]` (0-based), `max_us = arr[n-1]`.
- `txns = BENCH_THREADS * BENCH_TXNS_PER_THREAD`; `tps = txns / elapsed_s`;
  `qps = tps * 20` (18 statements + BEGIN + COMMIT, sysbench counting).

## Output: one JSON line on stdout

```json
{"lang":"c","driver":"libmysqlclient","driver_version":"8.0.46","runtime_version":"gcc 13.3.0","prepared":true,"client_host":"beast-node1","db_host":"127.0.0.1","threads":64,"txns":128000,"retries":31,"errors":0,"elapsed_s":42.137,"tps":3037.5,"qps":60750.4,"p50_us":19800,"p95_us":31200,"p99_us":44100,"max_us":215000}
```

`tps`/`qps` rounded to 1 decimal, `elapsed_s` to 3; latencies integers; `retries` =
sum over workers, **timed window only** (warmup transactions retry too, but those
retries are not reported — reset the counter after warmup); `errors` always 0
(anything non-retryable aborts).

## Canonical drivers (unchanged from v1)

| lang       | driver                    | workers                                   |
|------------|---------------------------|-------------------------------------------|
| c          | libmysqlclient C API      | pthreads                                  |
| cpp        | libmysqlclient C API      | std::thread                               |
| rust       | `mysql` crate 25.0.1      | std::thread                               |
| java       | Connector/J (lib/ jar)    | Thread + CyclicBarrier                    |
| python     | `mysqlclient` (venv)      | threading.Thread                          |
| php        | mysqli                    | pcntl_fork processes                      |
| go         | go-sql-driver/mysql       | goroutines, each with a dedicated sql.Conn |
| javascript | Node.js `mysql2`          | worker_threads, one connection per worker  |
| typescript | Node.js `mysql2` (tsc-compiled) | worker_threads, one connection per worker |
| ruby       | `mysql2` gem (libmysqlclient) | Thread (GVL released during queries, Python-style) |
| lua        | libmysqlclient via LuaJIT FFI (text protocol, prepared:false) | forked processes (no threads in Lua), PHP-style |
| csharp     | MySqlConnector (.NET 8)   | System.Threading.Thread, Pooling=false, cmd.Prepare() |
| swift      | libmysqlclient C API (C interop) | OS threads (pthread barrier), same API usage as C/C++ |

Go note: workers are goroutines (Go's native concurrency unit); each takes a dedicated
session via `db.Conn(ctx)` and prepares its statements on that conn — no pool sharing.
Set `db.SetMaxOpenConns(threads+1)` so the pool never caps the worker count.

JavaScript note: workers are `worker_threads` (real OS threads, one V8 isolate + one
mysql2 connection each); the parent coordinates the barrier via postMessage and merges
per-worker latency arrays. mysql2's `.execute()` is a server-side prepared statement;
BEGIN/COMMIT/ROLLBACK go through `.query()`.

Driver/TLS/urls details are identical to v1 (Java URL opts incl.
`useServerPrepStmts=true&cachePrepStmts=true&useSSL=false&allowPublicKeyRetrieval=true`;
note `prepStmtCacheSize` default 25 holds all 18 statements).
