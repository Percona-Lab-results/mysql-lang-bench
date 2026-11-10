# mysql-lang-bench

The same single-threaded MySQL OLTP workload (70% point SELECT / 20% UPDATE / 10% INSERT,
deterministic key sequence) implemented in C, C++, Rust, Java, Python and PHP. Each
language runs on its own host against that host's MySQL server; results are compared
by throughput and client-side latency percentiles.

What is actually being measured is **language runtime + canonical driver** overhead —
see `SPEC.md` for the exact contract every implementation follows (PRNG, SQL, timing,
output format) and the driver table.

## Setup

1. `ssh-add` your key (the harness uses `BatchMode=yes`, agent auth only).
2. `cp config/mysql.env.example config/mysql.env` and fill in credentials
   (the DB as reachable *from each benchmark host*, usually `127.0.0.1`).
3. Check the host→language mapping in `config/hosts.conf`.
4. On each host the language toolchain and MySQL client driver must be installable;
   `impl/<lang>/build.sh` reports what is missing.

## Usage

```bash
harness/run_all.sh                 # deploy+build+run everywhere, poll, collect, report

# or per host:
harness/deploy.sh 10.30.2.27
harness/build.sh  10.30.2.27
harness/run.sh    10.30.2.27       # detached; prints run id
harness/status.sh                  # latest status on every host
harness/collect.sh                 # pull results/ back
python3 report/generate_report.py  # markdown comparison table
```

Every run reloads the table (`harness/remote_prepare.sh`) so runs start from identical
data. Run artifacts land in `results/<run_id>.{status,json,log}` on the remote and are
mirrored to `results/<host>/` locally.
