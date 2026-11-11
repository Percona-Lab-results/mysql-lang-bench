# mysql-lang-bench

## Benchmarking 12 programming languages against the same MySQL workload

Every language community has its folklore about performance: C is fast, Python is
slow, the JVM warms up, Go scales effortlessly. Most of that folklore comes from
compute benchmarks — tight loops, ray tracers, n-body simulations. But that's not
what most code does for a living. Most code talks to a database. And once a network
round-trip and a database server enter the picture, the question "which language is
faster?" becomes a different and far more practical one: *how much does your
language and its MySQL driver actually cost you, per transaction, under real
concurrency?*

To answer it, we put twelve languages through an identical test: C, C++, Rust,
Swift and Go compiled to native code; Java and C# on managed runtimes; and PHP,
Python, Ruby, JavaScript and TypeScript representing the interpreted and
JIT-compiled scripting world. Each implementation runs the same sysbench-style
`oltp_read_write` transaction — ten point selects, four range queries, two updates,
and a delete-plus-insert — against the same Percona Server 8.4 instance and a
10-million-row table, scaling from 4 to 512 concurrent workers. Fairness is
enforced by construction, not by good intentions: every implementation draws the
same pseudo-random operation sequence from an identical generator, follows the same
prepared-statement and transaction rules, measures with the same
barrier-synchronized timing, and must reproduce a reference sequence before its
results count. Each language uses its canonical MySQL driver, because in practice
you don't benchmark a language — you benchmark a language plus the driver its
ecosystem actually uses.

We also measured something most language comparisons skip: where the CPU time
goes. By accounting client and server CPU separately for every run, we can tell
*why* a language is slow — whether it saturates the database like the native tier
does, burns its own cycles parsing wire protocol, or suffocates on a runtime lock
while the server sits idle.

The results hold surprises for every camp. A scripting language keeps pace with C
almost to the server's saturation point. A systems language celebrated for
concurrency collapses to a fifth of its potential on runtime defaults. Two
languages that share nothing but a compiler turn out to be performance-identical.
And at the top of the chart, the fastest languages stop mattering at all — because
past 128 connections, the only thing left to benchmark is the database itself.

## What this repository contains

- **`SPEC.md`** — the cross-language contract every implementation follows exactly:
  per-worker PRNG (with a validation sequence), transaction shape and SQL,
  connection and prepared-statement rules, barrier timing, deadlock retry policy,
  and the result JSON schema.
- **`impl/<lang>/`** — the 12 implementations (C, C++, Rust, Swift, Go, Java, C#,
  PHP, Python, Ruby, JavaScript, TypeScript), each with its own `build.sh`/`run.sh`.
- **`harness/`** — SSH orchestration: deploy the Percona Server tarball and the
  benchmark code to each host, build, load the 10M-row table, run thread-count
  sweeps detached with status files, collect results, and capture client/server
  CPU accounting per run.
- **`results/`** — per-run JSON from every host plus the merged `summary.csv`.
- **`report/`** — `generate_report.py` (markdown tables + CSV) and
  `generate_html_report.py` → `report/report.html`, an interactive report with
  the TPS chart, the client-vs-server CPU chart, table views and findings.

## Setup

1. `ssh-add` your key (the harness uses `BatchMode=yes`, agent auth only).
2. `cp config/mysql.env.example config/mysql.env` and fill in credentials
   (the DB as reachable *from each benchmark host*, usually `127.0.0.1`).
3. Check the host→language mapping in `config/hosts.conf` (multiple languages on
   one host run sequentially, with a fresh data load between them).
4. Deploy the MySQL server to each host: `harness/deploy_server.sh <host>`
   (Percona Server tarball, datadir `~/servers/data`, config `config/server.my.cnf`).
5. Each host needs its language toolchain; `impl/<lang>/build.sh` reports what is
   missing.

## Usage

```bash
harness/run_all.sh                      # deploy+build+sweep everywhere, poll, collect, report

# or per host:
harness/deploy.sh    10.30.2.27
harness/build.sh     10.30.2.27 c
harness/run_sweep.sh 10.30.2.27 c       # detached 4→512 worker sweep; prints sweep id
harness/status.sh                       # latest status on every host
harness/collect.sh                      # pull results/ back
python3 report/generate_report.py       # markdown tables + results/summary.csv
python3 report/generate_html_report.py  # report/report.html
```

Each language's sweep loads the table once (10M rows, deterministic data), then
runs every thread count without reloading. Per-rung artifacts land in
`results/<sweep_id>-<lang>-t<N>.json` on the remote and are mirrored to
`results/<host>/` locally; each JSON carries throughput, latency percentiles,
retry counts, and client/server CPU seconds for the run.
