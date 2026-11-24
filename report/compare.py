#!/usr/bin/env python3
"""Compare a separate benchmark (see separate_langs.py) with the standard languages.

Separate benchmarks, such as python-mp, are kept out of runs/avg.json and the
standard reports. This tool averages their sweeps into runs/<lang>/avg.json
and prints side-by-side tables on request.

  # average every complete python-mp sweep under results/ into runs/python-mp/avg.json
  report/compare.py --write-avg python-mp

  # python-mp vs Python (default), throughput and client CPU per op
  report/compare.py python-mp

  # against chosen languages, or against all thirteen
  report/compare.py python-mp --with python,php,c
  report/compare.py python-mp --all --workload 2

For the full HTML report with the separate benchmark added, run
  BENCH_COMPARE=python-mp BENCH_DATASET=runs/avg.json report/generate_html_report.py
which writes to report/compare-python-mp/.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import separate_langs

ROOT = Path(__file__).resolve().parent.parent
WL = {1: ("Integer insert", "rows/s", "row"), 2: ("Integer read + sum", "rows/s", "row"),
      3: ("Blob insert + MD5", "blobs/s", "blob"), 4: ("Blob read + MD5", "blobs/s", "blob")}
NAMES = {"c": "C", "cpp": "C++", "swift": "Swift", "rust": "Rust", "php": "PHP",
         "java": "Java", "csharp": "C#", "go": "Go", "javascript": "JavaScript",
         "typescript": "TypeScript", "python": "Python", "ruby": "Ruby", "lua": "Lua",
         **separate_langs.DISPLAY}


def key(r):
    return f'{r["workload"]}|{r["lang"]}|{r.get("tuned")}|{r["threads"]}'


def write_avg(lang, sweeps_wanted):
    """Average complete sweeps of `lang` from results/<host>/ into runs/<lang>/avg.json."""
    pat = re.compile(rf"^(sweep-.+?)-{re.escape(lang)}-w(\d)-t(\d+)\.json$")
    by_sweep = defaultdict(list)
    for f in ROOT.glob("results/*/*.json"):
        m = pat.match(f.name)
        if not m:
            continue
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if r.get("lang") == lang and "throughput" in r:
            by_sweep[f"{f.parent.name}/{m.group(1)}"].append(r)
    if not by_sweep:
        sys.exit(f"no {lang} sweep results under results/*/ (collect them first)")
    full = max(len(v) for v in by_sweep.values())
    sweeps = sorted(s for s, v in by_sweep.items() if len(v) == full)
    if sweeps_wanted:
        sweeps = [s for s in sweeps if any(w in s for w in sweeps_wanted)]
    if not sweeps:
        sys.exit("no complete sweeps match")
    cells = defaultdict(list)
    for s in sweeps:
        for r in by_sweep[s]:
            cells[key(r)].append(r)
    avg = {}
    for k, rs in sorted(cells.items()):
        out = dict(rs[-1])
        for f, v in rs[0].items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and f not in ("workload", "threads"):
                mean = sum(r[f] for r in rs) / len(rs)
                out[f] = round(mean, 3 if f == "elapsed_s" else 2 if f.endswith("_s") else 1) \
                    if isinstance(v, float) or f in ("throughput", "mb_per_s") else round(mean)
        out["_n"] = len(rs)
        avg[k] = out
    dest = ROOT / separate_langs.SEPARATE_LANGS[lang]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(avg, indent=1) + "\n")
    print(f"wrote {dest} from {len(sweeps)} sweep(s): {', '.join(sweeps)}", file=sys.stderr)


def fmt(v):
    return "-" if v is None else f"{v:,.0f}" if v >= 100 else f"{v:,.1f}" if v >= 10 else f"{v:.2f}"


def compare(lang, others, workloads):
    base = json.loads((ROOT / "runs" / "avg.json").read_text())
    sep_path = ROOT / separate_langs.SEPARATE_LANGS[lang]
    if not sep_path.exists():
        sys.exit(f"{sep_path} not found: run report/compare.py --write-avg {lang}")
    data = {**base, **json.loads(sep_path.read_text())}
    by = {}
    for r in data.values():
        by[(r["workload"], r["lang"], r["threads"])] = r
    langs = [lang] + others
    for w in workloads:
        title, unit, per = WL[w]
        threads = sorted({t for (wl, l, t) in by if wl == w and l == lang})
        if not threads:
            continue
        for metric, label in (("thr", f"throughput, {unit}"),
                              ("cpu", f"client CPU µs per {per}"),
                              ("cores", "client cores busy")):
            print(f"\n### W{w} {title} — {label}\n")
            print("| language | " + " | ".join(str(t) for t in threads) + " |")
            print("|---|" + "---:|" * len(threads))
            for l in langs:
                cells = []
                for t in threads:
                    r = by.get((w, l, t))
                    if r is None:
                        cells.append(None)
                    elif metric == "thr":
                        cells.append(r["throughput"])
                    elif metric == "cpu":
                        cells.append(r["client_cpu_s"] * 1e6 / r["ops"] if r.get("ops") else None)
                    else:
                        wall = r.get("run_wall_s") or r.get("elapsed_s")
                        cells.append(r["client_cpu_s"] / wall if wall else None)
                print(f"| {NAMES.get(l, l)} | " + " | ".join(fmt(c) for c in cells) + " |")
            if metric == "thr" and lang == "python-mp" and "python" in langs:
                ratios = []
                for t in threads:
                    a, b = by.get((w, lang, t)), by.get((w, "python", t))
                    ratios.append(f"{a['throughput'] / b['throughput']:.1f}×" if a and b else "-")
                print("| *vs Python* | " + " | ".join(ratios) + " |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lang", choices=sorted(separate_langs.SEPARATE_LANGS))
    ap.add_argument("--with", dest="others", default="python",
                    help="comma-separated standard languages to compare with (default: python)")
    ap.add_argument("--all", action="store_true", help="compare with all standard languages")
    ap.add_argument("--workload", type=int, action="append", choices=[1, 2, 3, 4])
    ap.add_argument("--write-avg", action="store_true",
                    help="average complete sweeps from results/ into runs/<lang>/avg.json")
    ap.add_argument("--sweep", action="append", default=[],
                    help="with --write-avg: only sweeps whose id contains this text (repeatable)")
    a = ap.parse_args()
    if a.write_avg:
        write_avg(a.lang, a.sweep)
        return
    if a.all:
        others = ["c", "cpp", "swift", "rust", "go", "java", "csharp", "php", "lua",
                  "javascript", "typescript", "python", "ruby"]
    else:
        others = [o.strip() for o in a.others.split(",") if o.strip()]
    compare(a.lang, others, a.workload or [1, 2, 3, 4])


if __name__ == "__main__":
    main()
