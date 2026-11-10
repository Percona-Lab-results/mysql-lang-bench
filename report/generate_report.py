#!/usr/bin/env python3
"""Merge results/<host>/*.json into markdown tables (stdout).

v2 results carry threads/tps/txns; rows are grouped by language and sorted by
thread count, plus a tps-by-threads pivot across languages.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def load() -> list:
    rows = []
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"skipping {f}: {e}", file=sys.stderr)
            continue
        if "threads" not in r:
            continue  # v1 single-threaded result, not comparable
        r["_run"] = f.stem
        r["_host"] = f.parent.name
        rows.append(r)
    return rows

def main() -> None:
    rows = load()
    if not rows:
        print("no v2 results found under results/*/", file=sys.stderr)
        sys.exit(1)

    # keep the latest result per (lang, threads)
    latest = {}
    for r in rows:
        latest[(r["lang"], r["threads"])] = r
    rows = sorted(latest.values(), key=lambda r: (r["lang"], r["threads"]))

    print("## Full results\n")
    print("| lang | threads | tps | qps | p50 us | p95 us | p99 us | max us | retries | elapsed s | cli cpu s | srv cpu s | run |")
    print("|------|--------:|----:|----:|-------:|-------:|-------:|-------:|--------:|----------:|----------:|----------:|-----|")
    for r in rows:
        print(
            f"| {r['lang']} | {r['threads']} | {r['tps']:,} | {r['qps']:,} "
            f"| {r['p50_us']:,} | {r['p95_us']:,} | {r['p99_us']:,} | {r['max_us']:,} "
            f"| {r.get('retries', 0)} | {r['elapsed_s']} "
            f"| {r.get('client_cpu_s', '-')} | {r.get('server_cpu_s', '-')} | {r['_run']} |"
        )

    # CPU split pivot: client CPU microseconds per transaction (and server's),
    # only for rows that carry CPU data.
    cpu_rows = [r for r in rows if "client_cpu_s" in r]
    if cpu_rows:
        threads_c = sorted({r["threads"] for r in cpu_rows})
        langs_c = sorted({r["lang"] for r in cpu_rows})
        for key, title in (("client_cpu_s", "Client CPU per txn (us)"),
                           ("server_cpu_s", "Server CPU per txn (us)")):
            print(f"\n## {title}\n")
            print("| lang | " + " | ".join(str(t) for t in threads_c) + " |")
            print("|------|" + "---:|" * len(threads_c))
            for lang in langs_c:
                cells = []
                for t in threads_c:
                    m = [r for r in cpu_rows if r["lang"] == lang and r["threads"] == t]
                    cells.append(f"{m[0][key] / m[0]['txns'] * 1e6:,.0f}" if m else "-")
                print(f"| {lang} | " + " | ".join(cells) + " |")

    threads = sorted({r["threads"] for r in rows})
    langs = sorted({r["lang"] for r in rows})
    by = defaultdict(dict)
    for r in rows:
        by[r["lang"]][r["threads"]] = r["tps"]

    print("\n## TPS by thread count\n")
    print("| lang | " + " | ".join(str(t) for t in threads) + " |")
    print("|------|" + "---:|" * len(threads))
    for lang in langs:
        cells = [f"{by[lang].get(t, '-'):,}" if isinstance(by[lang].get(t), (int, float)) else "-" for t in threads]
        print(f"| {lang} | " + " | ".join(cells) + " |")

    print("\nDrivers: " + "; ".join(
        f"{lang}={next(r for r in rows if r['lang'] == lang)['driver']} "
        f"{next(r for r in rows if r['lang'] == lang)['driver_version']}"
        for lang in langs))

    # Merged CSV for downstream processing.
    csv_path = ROOT / "results" / "summary.csv"
    cols = ["lang", "threads", "tps", "qps", "p50_us", "p95_us", "p99_us",
            "max_us", "retries", "errors", "txns", "elapsed_s",
            "client_cpu_s", "server_cpu_s", "run_wall_s", "driver",
            "driver_version", "runtime_version", "prepared", "client_host",
            "_host", "_run"]
    with csv_path.open("w") as f:
        f.write(",".join(c.lstrip("_") for c in cols) + "\n")
        for r in rows:
            f.write(",".join(f'"{r.get(c, "")}"' if "," in str(r.get(c, "")) else str(r.get(c, "")) for c in cols) + "\n")
    print(f"\nCSV written: {csv_path}", file=sys.stderr)

if __name__ == "__main__":
    main()
