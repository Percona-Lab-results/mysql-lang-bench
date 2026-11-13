# Benchmark implementation contract (v3 — four micro-workloads)

Each implementation under `impl/<lang>/` runs **one** of four workloads, selected by
`BENCH_WORKLOAD` (1–4), with `BENCH_THREADS` concurrent workers, and prints one JSON
line. All implementations MUST follow this contract exactly so results are comparable.

There is no pre-warming. Workloads 1 and 3 are inserts and must run before the
matching read workloads (2 and 4) at the same worker count; the harness orders them.

## Files each implementation provides

- `impl/<lang>/build.sh` — builds/installs deps on the host. Idempotent; exits non-zero
  with a one-line hint when something is missing.
- `impl/<lang>/run.sh` — runs the benchmark; config from env vars only; one JSON line on
  stdout, diagnostics on stderr. Both invoked from the repo root.

## Workers, connections, timing (common to all workloads)

- `BENCH_THREADS` workers: OS threads (C pthreads, C++/Rust std::thread, Java Thread,
  C# Thread, Python threading, Go goroutines each with a dedicated `sql.Conn`, Node/TS
  worker_threads, Ruby Thread, Lua/PHP forked processes). Each worker owns its own
  connection; no pooling/sharing.
- TCP to `MYSQL_HOST:MYSQL_PORT`; TLS disabled (libmysqlclient-based drivers set
  SSL_MODE_DISABLED + GET_SERVER_PUBLIC_KEY). Autocommit ON; each worker commits its
  own work (see per-workload commit rules).
- **Per-worker counts**: every worker performs the full amount below (work scales with
  worker count). Nothing is shared/divided across workers.
- **Barrier + timing**: each worker connects, prepares statements, then waits at a start
  barrier. Main releases the barrier, samples a monotonic clock (t_start), joins all
  workers, samples t_end. `elapsed_s = t_end - t_start`. Connection/prepare are outside
  the timed window. There is no warm-up phase inside the program.
- Per-operation latency in **integer microseconds** (truncated) into a per-worker array;
  merge across workers, sort, `p(x) = arr[ceil(x/100 * n) - 1]`, `max_us = arr[n-1]`.

## Environment variables

Connection (`config/mysql.env`): `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`,
`MYSQL_PASSWORD`, `MYSQL_DB`.

Workload (`common/workload.env`, integers):

- `BENCH_WORKLOAD`    — 1..4 (which workload to run)
- `BENCH_THREADS`     — worker count
- `BENCH_SEED`        — base PRNG seed (worker i seeds `BENCH_SEED + i`)
- `BENCH_INT_ROWS`    — rows per worker for workloads 1 & 2 (default 1000000)
- `BENCH_INT_BATCH`   — rows per INSERT statement for workload 1 (default 10)
- `BENCH_BLOB_COUNT`  — blobs per worker for workloads 3 & 4 (default 100)
- `BENCH_BLOB_SIZE`   — bytes per blob (default 1048576 = 1 MiB)

## PRNG — per worker

31-bit LCG. Worker `i` seeds `state = BENCH_SEED + i`:
`next(): state = (state*1103515245 + 12345) mod 2^31; return state`. Used for random
int values and blob byte fill. (Validation, seed 42 worker 0: first `next()` =
1250496027.)

## Tables (created once by the harness, see common/schema.sql)

```sql
CREATE TABLE bench_int  (val INT NOT NULL) ENGINE=InnoDB;                       -- no secondary index: max insert speed
CREATE TABLE bench_blob (id BIGINT NOT NULL PRIMARY KEY, data LONGBLOB NOT NULL) ENGINE=InnoDB;
```

## The four workloads

### Workload 1 — integer insert (write)
Each worker inserts `BENCH_INT_ROWS` rows into `bench_int` using one **prepared** multi-row
INSERT of `BENCH_INT_BATCH` rows: `INSERT INTO bench_int (val) VALUES (?),(?),…,(?)`
(BENCH_INT_BATCH placeholders), executed `BENCH_INT_ROWS / BENCH_INT_BATCH` times. Each
bound value is a fresh `next()` cast to a signed 32-bit int. **Autocommit ON — each INSERT
is its own commit; do NOT wrap the load in a single BEGIN/COMMIT transaction** (the server
does not fsync per commit, per its config — "no disk sync after each commit"). This commit
strategy must be identical in every language. The harness **truncates `bench_int` before
this workload**. One latency sample per executed INSERT statement. Reported `ops` = total rows inserted (threads × BENCH_INT_ROWS);
`throughput` = ops / elapsed_s (rows/sec); unit "rows/s".

### Workload 2 — integer fetch + sum (read)
Each worker runs `SELECT val FROM bench_int LIMIT BENCH_INT_ROWS`, fetches every row, and
**sums `val` in application code** (64-bit accumulator; feed the sum to a black-box sink so
it is not optimized away). Prepared statement. The table already holds the rows inserted by
workload 1 at the same worker count (the harness runs 1 then 2 without truncating between
them); the server has them cached — no warm-up. One latency sample per worker for the whole
scan (n = threads). Reported `ops` = total rows read; `throughput` = rows/sec; unit "rows/s".

### Workload 3 — blob insert + MD5 (write)
Each worker, for `BENCH_BLOB_COUNT` iterations: produce a `BENCH_BLOB_SIZE`-byte buffer of
pseudo-random content, compute the **MD5 of the whole buffer in app code** (digest consumed
into a sink), then `INSERT INTO bench_blob (id, data) VALUES (?, ?)` with
`id = i * BENCH_BLOB_COUNT + k` (worker i, blob k — a contiguous per-worker id range) via a
prepared statement.

**Content generation must be cheap and comparable, NOT a per-byte interpreted loop** (that
would make a scripting language's workload measure loop speed, not MD5/DB throughput).
Generate the buffer **once per worker** using the language's fast native facility for a
pseudo-random byte buffer (e.g. `random.randbytes`, `Random.nextBytes`, `crypto.randomFillSync`,
`RAND_bytes`, or a bulk LCG fill writing ≥8 bytes per step in native/compiled code), then
before each blob overwrite its first 8 bytes with the blob id so every blob's content (and
MD5) differs. The content need not be identical across languages. MD5 processes the full
`BENCH_BLOB_SIZE` each time. **Autocommit ON — each blob INSERT is its own commit, no
wrapping transaction** (same rule as workload 1); identical in every language. The harness
**truncates `bench_blob` before this workload**. One latency sample per blob insert.
Reported `ops` = total blobs (threads × BENCH_BLOB_COUNT); `throughput` = blobs/sec; also
`mb_per_s` = ops * BENCH_BLOB_SIZE / 1e6 / elapsed_s. unit "blobs/s".

### Workload 4 — blob read + MD5 (read)
Each worker reads **its own** `BENCH_BLOB_COUNT` blobs by id range
(`SELECT data FROM bench_blob WHERE id = ?`, ids `i*BENCH_BLOB_COUNT + k`), and computes the
**MD5 of each blob's content in app code** (digest to a sink). Prepared statement. Runs after
workload 3 at the same worker count (data present, cached). One latency sample per blob read.
Reported `ops` = total blobs read; `throughput` = blobs/sec; `mb_per_s` as above; unit "blobs/s".

## Intermediate progress (kill / long-run recovery)

So a run that is killed or runs too long is still partially recoverable, each
program checkpoints its progress while the timed phase runs:

- Env: `BENCH_PROGRESS_FILE` (path; if empty/unset, skip progress) and
  `BENCH_PROGRESS_SEC` (default 5).
- Roughly every `BENCH_PROGRESS_SEC` seconds during the timed phase, overwrite
  `BENCH_PROGRESS_FILE` **atomically** (write `FILE.tmp`, then rename over FILE)
  with one JSON line:
  `{"workload":W,"workload_name":"…","unit":"…","threads":N,"ops":<ops so far>,"elapsed_s":<now - t_start>,"throughput":<ops_so_far / elapsed>,"partial":true}`
  where `ops so far` = completed operations summed across all workers
  (operations = rows for WL1/2, blobs for WL3/4). Slightly-stale counts are fine.
- On successful completion, print the full final JSON to stdout as usual and
  delete `BENCH_PROGRESS_FILE` (its data is now superseded by stdout).
- Implementation: threaded languages — each worker publishes its own completed
  count to a per-worker slot it alone writes (no lock); main samples the slots
  on a timer and writes the file. Forked languages (PHP, Lua) — each child
  periodically writes its own count to a per-child temp; the parent polls
  (non-blocking waitpid + sleep), sums, and writes the progress file.

The harness sets `BENCH_PROGRESS_FILE` per run and removes it after the result
JSON is captured; a `.progress` file left behind marks an interrupted run whose
partial throughput can still be read.

## Output: one JSON line on stdout

```json
{"lang":"c","workload":1,"workload_name":"int-insert","unit":"rows/s","driver":"libmysqlclient","driver_version":"8.0.46","runtime_version":"gcc 13.3.0","prepared":true,"client_host":"beast-node1","db_host":"127.0.0.1","threads":64,"ops":64000000,"errors":0,"elapsed_s":42.1,"throughput":1520190.0,"mb_per_s":0,"p50_us":240,"p95_us":610,"p99_us":880,"max_us":15200,"client_cpu_s":0,"server_cpu_s":0}
```

`throughput`/`mb_per_s` rounded to 1 decimal, `elapsed_s` to 3; latencies integers;
`errors` always 0 (any SQL/connection error → stderr + non-zero exit, no retries — these
workloads have no concurrency conflicts to retry). `client_cpu_s`/`server_cpu_s` are
filled by the harness, not the program (leave 0).

## Canonical drivers (unchanged from v2)

c/cpp/swift = libmysqlclient; rust = `mysql` crate; java = Connector/J; csharp =
MySqlConnector; php = mysqli; python = mysqlclient; ruby = mysql2; lua = libmysqlclient
via LuaJIT FFI; javascript/typescript = mysql2 (Node). Go/C# additionally run a tuned
variant (Go `GOMAXPROCS=16 GOGC=800`; C# server GC) kept as a separate benchmark.
