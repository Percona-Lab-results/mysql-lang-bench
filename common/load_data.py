#!/usr/bin/env python3
"""Emit SQL on stdout that loads sbtest with N deterministic rows (ids 1..N).

Usage: load_data.py <table_rows>   |  pipe into the mysql client.
Uses the same LCG as the benchmarks (see SPEC.md) with a fixed loader seed of 1,
so every host loads byte-identical data. Commits every COMMIT_EVERY rows so the
undo log stays bounded for large (multi-million row) loads.
"""
import sys

C_VAL = "abcdefghij" * 12
PAD_VAL = "0123456789" * 6
BATCH = 10000
COMMIT_EVERY = 100000

state = 1  # loader seed, independent of BENCH_SEED

def rand(n: int) -> int:
    global state
    state = (state * 1103515245 + 12345) % 2147483648
    return state % n

def main() -> None:
    rows = int(sys.argv[1])
    out = sys.stdout
    out.write("SET autocommit=0;\nSET unique_checks=0;\n")
    since_commit = 0
    for start in range(1, rows + 1, BATCH):
        end = min(start + BATCH, rows + 1)
        values = ",".join(
            f"({i},{rand(rows)},'{C_VAL}','{PAD_VAL}')" for i in range(start, end)
        )
        out.write(f"INSERT INTO sbtest (id,k,c,pad) VALUES {values};\n")
        since_commit += end - start
        if since_commit >= COMMIT_EVERY:
            out.write("COMMIT;\n")
            since_commit = 0
    out.write("COMMIT;\nSET unique_checks=1;\nANALYZE TABLE sbtest;\n")

if __name__ == "__main__":
    main()
