#!/usr/bin/env python3
"""Merge results/<host>/*.json into markdown tables (stdout), one block per
benchmark mode (work-based "events" vs "duration"), plus results/summary.csv.

Only mode-tagged rows (runs made after modes were introduced) are included.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODE_TITLE = {"events": "Work-based mode", "duration": "Fixed-duration mode"}


def load():
    latest = {}
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"skipping {f}: {e}", file=sys.stderr)
            continue
        if "threads" not in r or "mode" not in r:
            continue
        r["_run"] = f.stem
        r["_host"] = f.parent.name
        # Keep stock and tuned runs (Go/C#) as distinct rows.
        variant = r.get("tuned")
        r["_disp"] = r["lang"] + (" (optimized)" if variant is True else
                                  " (stock)" if variant is False else "")
        latest[(r["mode"], r["lang"], variant, r["threads"])] = r
    return sorted(latest.values(), key=lambda r: (r["mode"], r["_disp"], r["threads"]))


def main() -> None:
    rows = [r for r in load() if r["mode"] == "events"]  # work-based only
    if not rows:
        print("no work-based results found under results/*/", file=sys.stderr)
        sys.exit(1)

    for mode in sorted({r["mode"] for r in rows}):
        mrows = [r for r in rows if r["mode"] == mode]
        threads = sorted({r["threads"] for r in mrows})
        langs = sorted({r["_disp"] for r in mrows})
        dur = next((r["duration_s"] for r in mrows if r.get("duration_s")), 0)
        title = MODE_TITLE.get(mode, mode)
        if mode == "duration":
            title += f" ({dur}s per rung)"
        print(f"\n# {title}\n")
        print("## Full results\n")
        print("| lang | threads | tps | qps | p50 us | p95 us | p99 us | max us | retries | txns | elapsed s | cli cpu s | srv cpu s |")
        print("|------|--------:|----:|----:|-------:|-------:|-------:|-------:|--------:|-----:|----------:|----------:|----------:|")
        for r in mrows:
            print(
                f"| {r['_disp']} | {r['threads']} | {r['tps']:,} | {r['qps']:,} "
                f"| {r['p50_us']:,} | {r['p95_us']:,} | {r['p99_us']:,} | {r['max_us']:,} "
                f"| {r.get('retries', 0)} | {r['txns']:,} | {r['elapsed_s']} "
                f"| {r.get('client_cpu_s', '-')} | {r.get('server_cpu_s', '-')} |"
            )
        by = defaultdict(dict)
        for r in mrows:
            by[r["_disp"]][r["threads"]] = r["tps"]
        print("\n## TPS by thread count\n")
        print("| lang | " + " | ".join(str(t) for t in threads) + " |")
        print("|------|" + "---:|" * len(threads))
        for lang in langs:
            cells = [f"{by[lang][t]:,}" if t in by[lang] else "-" for t in threads]
            print(f"| {lang} | " + " | ".join(cells) + " |")

    # merged CSV
    csv_path = ROOT / "results" / "summary.csv"
    cols = ["mode", "lang", "tuned", "threads", "tps", "qps", "p50_us", "p95_us",
            "p99_us", "max_us", "retries", "errors", "txns", "elapsed_s",
            "client_cpu_s", "server_cpu_s", "run_wall_s", "duration_s",
            "driver", "driver_version", "runtime_version", "prepared",
            "client_host", "_host", "_run"]
    with csv_path.open("w") as f:
        f.write(",".join(c.lstrip("_") for c in cols) + "\n")
        for r in rows:
            f.write(",".join(
                f'"{r.get(c, "")}"' if "," in str(r.get(c, "")) else str(r.get(c, ""))
                for c in cols) + "\n")
    print(f"\nCSV written: {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
