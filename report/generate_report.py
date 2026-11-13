#!/usr/bin/env python3
"""Merge results/<host>/*.json into markdown tables (stdout), one block per
workload (v3), plus results/summary.csv."""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WL = {1: "Workload 1 — integer insert (rows/s)",
      2: "Workload 2 — integer fetch + sum (rows/s)",
      3: "Workload 3 — blob insert + MD5 (blobs/s)",
      4: "Workload 4 — blob read + MD5 (blobs/s)"}


def load():
    latest = {}
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "workload" not in r or "threads" not in r:
            continue
        r["_run"] = f.stem
        r["_host"] = f.parent.name
        v = r.get("tuned")
        r["_disp"] = r["lang"] + (" (optimized)" if v is True else " (stock)" if v is False else "")
        latest[(r["workload"], r["lang"], v, r["threads"])] = r
    return sorted(latest.values(), key=lambda r: (r["workload"], r["_disp"], r["threads"]))


def main():
    rows = load()
    if not rows:
        print("no v3 workload results found under results/*/", file=sys.stderr)
        sys.exit(1)

    for w in (1, 2, 3, 4):
        wr = [r for r in rows if r["workload"] == w]
        if not wr:
            continue
        threads = sorted({r["threads"] for r in wr})
        langs = sorted({r["_disp"] for r in wr})
        print(f"\n# {WL.get(w, 'Workload ' + str(w))}\n")
        by = defaultdict(dict)
        for r in wr:
            by[r["_disp"]][r["threads"]] = r["throughput"]
        print("| language | " + " | ".join(str(t) for t in threads) + " |")
        print("|----------|" + "---:|" * len(threads))
        for lang in langs:
            cells = [f"{by[lang][t]:,.0f}" if t in by[lang] else "-" for t in threads]
            print(f"| {lang} | " + " | ".join(cells) + " |")

    csv_path = ROOT / "results" / "summary.csv"
    cols = ["workload", "workload_name", "unit", "lang", "tuned", "threads",
            "throughput", "mb_per_s", "ops", "errors", "elapsed_s",
            "p50_us", "p95_us", "p99_us", "max_us", "client_cpu_s",
            "server_cpu_s", "run_wall_s", "driver", "driver_version",
            "runtime_version", "prepared", "client_host", "_host", "_run"]
    with csv_path.open("w") as f:
        f.write(",".join(c.lstrip("_") for c in cols) + "\n")
        for r in rows:
            f.write(",".join(
                f'"{r.get(c, "")}"' if "," in str(r.get(c, "")) else str(r.get(c, ""))
                for c in cols) + "\n")
    print(f"\nCSV written: {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
