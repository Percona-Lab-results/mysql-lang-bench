"""Benchmarks kept out of the standard reports unless asked for.

A "separate" benchmark (for now only python-mp, the multi-process Python
variant in impl/python-mp) writes ordinary result JSON with its own `lang`
value, but the standard reports and the blog charts leave it out, so the
13-language results are unchanged. Its averaged results live in their own
file, runs/<lang>/avg.json, never in runs/avg.json.

To compare on request, set BENCH_COMPARE to a comma- or space-separated list
of separate benchmarks, for example:

    BENCH_COMPARE=python-mp python3 report/generate_html_report.py

The report generators then include those benchmarks (from results/ or, in
BENCH_DATASET mode, from runs/<lang>/avg.json) and write to
report/compare-<langs>/ instead of over the standard pages.
For a quick text comparison use report/compare.py.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# lang -> averaged dataset file (relative to the repo root)
SEPARATE_LANGS = {"python-mp": "runs/python-mp/avg.json"}

DISPLAY = {"python-mp": "Python-MP"}  # short: fits chart end labels

# One-paragraph explanation shown at the top of comparison pages.
NOTES = {
    "python-mp": (
        "<strong>Python-MP</strong> is a separate benchmark, not part of the standard "
        "13-language comparison: the same four workloads with the same Python driver "
        "(mysqlclient), but every worker is its own process (as in PHP and Lua) instead of "
        "a thread, plus tuning: mysqlclient's low-level C API instead of cursors, blobs "
        "sent as hex literals instead of escaped strings, built-in summing, and the "
        "garbage collector off in workers. Average of three runs. "
        "<strong>Python</strong> is the standard threaded implementation."),
}


def compare_langs():
    raw = os.environ.get("BENCH_COMPARE", "").replace(",", " ").split()
    unknown = [l for l in raw if l not in SEPARATE_LANGS]
    if unknown:
        raise SystemExit(f"BENCH_COMPARE: unknown separate benchmark(s) {unknown}; "
                         f"known: {sorted(SEPARATE_LANGS)}")
    return sorted(set(raw))


def keep(lang):
    """True if records of this lang belong in the current report."""
    return lang not in SEPARATE_LANGS or lang in compare_langs()


def dataset_records():
    """Averaged records of the requested separate benchmarks (BENCH_DATASET mode)."""
    out = []
    for lang in compare_langs():
        p = ROOT / SEPARATE_LANGS[lang]
        if not p.exists():
            raise SystemExit(f"{p} not found: average the {lang} runs first "
                             f"(report/compare.py --write-avg {lang})")
        raw = json.loads(p.read_text())
        out.extend(raw.values() if isinstance(raw, dict) else raw)
    return out


def output_dir(default):
    """Where report pages go: the standard dir, or report/compare-<langs>/."""
    langs = compare_langs()
    if not langs:
        return default
    d = default / ("compare-" + "+".join(langs))
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_note(html):
    """Insert the requested benchmarks' notes after the page's first </h1>."""
    langs = compare_langs()
    if not langs:
        return html
    note = "".join(f'<p class="context" style="border-left:3px solid currentColor;'
                   f'padding-left:10px">{NOTES[l]}</p>' for l in langs if l in NOTES)
    return html.replace("</h1>", "</h1>" + note, 1)
