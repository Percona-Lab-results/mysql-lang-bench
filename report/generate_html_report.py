#!/usr/bin/env python3
"""Generate the v3 four-workload report pages plus light-mode PNGs.

Two HTML pages share one template and dataset:
  * report.html — one throughput-vs-worker-count line chart per workload (1..4).
  * bars.html   — one grouped horizontal bar chart per workload: worker-count
    groups stacked 512-on-top down to 4, bars sorted best->worst within each
    group and labelled "<throughput> - <language>".

Go and C# are run both stock and tuned, but only the stock curve is shown (one
curve per language). Every chart is exported as a light-mode PNG into
report/png/; bar PNGs are sized to their content so no group is cropped.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "report" / "report.html"
BARS_OUT = ROOT / "report" / "bars.html"
ANN_OUT = ROOT / "report" / "annotated.html"
RANK_OUT = ROOT / "report" / "ranked.html"
INFO_OUT = ROOT / "report" / "infographic.html"
PNG_DIR = ROOT / "report" / "png"

# PNG export geometry. PNG_W is the output image width in pixels; scale factor 1
# makes the screenshot exactly that wide. PNG_PAD is the horizontal padding the
# page adds around a chart in png mode (.wrap 20px + .bigchart 16px, both sides),
# so the chart SVG itself renders at PNG_W - PNG_PAD px.
PNG_W = 1024
PNG_SCALE = 1
PNG_PAD = 72
RENDER_W = PNG_W - PNG_PAD

# Categorical palette (dataviz slots 1..13), light and dark surfaces. Used for
# the per-language line charts and legends. (The bar charts colour by in-group
# performance instead, computing a green->red hue in the browser.)
PAL_LIGHT = {
    1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", 4: "#eda100", 5: "#e87ba4",
    6: "#008300", 7: "#6250d6", 8: "#e34948", 9: "#84b000", 10: "#0092c4",
    11: "#8c5a2b", 12: "#b5174e", 13: "#00847c",
}
PAL_DARK = {
    1: "#3987e5", 2: "#d95926", 3: "#199e70", 4: "#c98500", 5: "#d55181",
    6: "#008300", 7: "#9085e9", 8: "#e66767", 9: "#a3c636", 10: "#35b5e0",
    11: "#b07845", 12: "#d6447c", 13: "#2aaea5",
}


def cat_css(pal):
    """CSS declarations for the categorical hues."""
    return " ".join(f"--cat-{n}: {pal[n]};" for n in sorted(pal))

DISPLAY = {
    "c": "C", "cpp": "C++", "swift": "Swift", "rust": "Rust", "php": "PHP",
    "java": "Java", "csharp": "C#", "go": "Go", "javascript": "JavaScript",
    "typescript": "TypeScript", "python": "Python", "ruby": "Ruby", "lua": "Lua",
}
# workload id -> (title, subtitle)
WL = {
    1: ("Workload 1 — integer insert",
        "Each worker bulk-inserts single-int rows via 10-row prepared-statement batches (autocommit, no per-commit fsync). Throughput in rows/second."),
    2: ("Workload 2 — integer fetch + sum",
        "Each worker fetches its single-int rows and sums them in application code. Throughput in rows/second."),
    3: ("Workload 3 — blob insert + MD5",
        "Each worker generates 1 MiB random blobs, MD5s each in the app, and inserts them. Throughput in blobs/second."),
    4: ("Workload 4 — blob read + MD5",
        "Each worker reads its 1 MiB blobs and MD5s each in the app. Throughput in blobs/second."),
}


def load_rows():
    # Optional explicit dataset (e.g. a precomputed run1/run2 average): a JSON
    # object whose values are result records. Bypasses the results/ glob so the
    # report can render a derived dataset. Set BENCH_DATASET to its path.
    ds = os.environ.get("BENCH_DATASET")
    if ds:
        raw = json.loads(Path(ds).read_text())
        records = raw.values() if isinstance(raw, dict) else raw
        return {(r["workload"], r["lang"], r.get("tuned"), r["threads"]): r
                for r in records
                if all(k in r for k in ("workload", "threads", "throughput", "lang"))}
    latest = {}
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "workload" not in r or "threads" not in r or "throughput" not in r:
            continue
        latest[(r["workload"], r["lang"], r.get("tuned"), r["threads"])] = r
    return latest


def build_workload(latest, w):
    threads = sorted({t for (wl, _, _, t) in latest if wl == w})
    groups = sorted({(l, v) for (wl, l, v, _) in latest if wl == w})
    if not threads or not groups:
        return None
    unit = "ops/s"
    series = []
    for lang, variant in groups:
        thr = []
        cpu = []   # client (benchmark process) CPU microseconds per operation
        for t in threads:
            r = latest.get((w, lang, variant, t))
            thr.append(round(r["throughput"], 1) if r else None)
            if r:
                unit = r.get("unit", unit)
                ops = r.get("ops") or 0
                cc = r.get("client_cpu_s")
                cpu.append(round(cc * 1e6 / ops, 2) if (ops and cc is not None) else None)
            else:
                cpu.append(None)
        if any(v is not None for v in thr):
            series.append({
                "lang": lang,
                "name": DISPLAY.get(lang, lang),
                "variant": "stock" if variant is False else ("tuned" if variant is True else "na"),
                "thr": thr,
                "cpu": cpu,
                "peak": max(v for v in thr if v is not None),
            })
    if not series:
        return None
    series.sort(key=lambda s: -s["peak"])
    title, sub = WL.get(w, (f"Workload {w}", ""))
    return {"threads": threads, "series": series, "unit": unit,
            "title": title, "sub": sub}


def table(series, threads):
    head = "".join(f'<th class="num">{t}</th>' for t in threads)
    body = ""
    for s in series:
        suf = {"stock": " (stock)", "tuned": " (optimized)"}.get(s["variant"], "")
        cells = "".join(
            f'<td class="num">{v:,.0f}</td>' if v is not None else '<td class="num">–</td>'
            for v in s["thr"])
        body += f'<tr><th scope="row">{s["name"]}{suf}</th>{cells}</tr>\n'
    return (f'<table><thead><tr><th>Language</th>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


# Palette slot per language (matches the chart STYLE map) so infographic bars
# use each language's chart colour.
SLOT = {"c": 1, "cpp": 2, "swift": 3, "rust": 8, "php": 5, "java": 6,
        "csharp": 11, "go": 10, "javascript": 4, "typescript": 9, "python": 7,
        "ruby": 12, "lua": 13}

# Plain-language framing per workload for the infographic.
INFO_WL = {
    1: ("\U0001F4E5", "Insert integer rows", "rows inserted / sec"),
    2: ("\U0001F4D6", "Read &amp; sum integer rows", "rows read / sec"),
    3: ("\U0001F4E6", "Insert 1 MB blobs", "blobs written / sec"),
    4: ("\U0001F4E4", "Read 1 MB blobs", "blobs read / sec"),
}
MEDALS = {0: "\U0001F947", 1: "\U0001F948", 2: "\U0001F949"}


def pf(v):
    """Compact, human number: 29.4M, 188K, 335."""
    if v >= 1e6:
        return f"{v/1e6:.1f}M"
    if v >= 1e5:
        return f"{v/1e3:.0f}K"
    if v >= 1e3:
        return f"{v/1e3:.1f}K"
    return f"{v:.0f}"


def build_infographic(workloads):
    """A plain-language, at-a-glance visual summary: per workload a ranked bar
    list (winner = full bar) with medals, plus headline stat callouts."""
    # Headline callouts.
    best = None            # (peak, lang_name, workload)
    spreads = []           # (ratio, workload, winner, slowest)
    for w, m in workloads.items():
        ser = [s for s in m["series"] if s["peak"] is not None]
        if not ser:
            continue
        top = max(ser, key=lambda s: s["peak"])
        bot = min(ser, key=lambda s: s["peak"])
        if best is None or top["peak"] > best[0]:
            best = (top["peak"], top["name"], w)
        if bot["peak"] > 0:
            spreads.append((top["peak"]/bot["peak"], w, top, bot))
    widest = max(spreads, key=lambda x: x[0]) if spreads else None
    evenest = min(spreads, key=lambda x: x[0]) if spreads else None

    def callout(big, label, sub):
        return (f'<div class="stat"><div class="stat-big">{big}</div>'
                f'<div class="stat-label">{label}</div>'
                f'<div class="stat-sub">{sub}</div></div>')

    cards = []
    if best:
        cards.append(callout(pf(best[0]), "Fastest single result",
                             f"{best[1]} — {INFO_WL[best[2]][1]}"))
    if widest:
        _, w, top, bot = widest
        cards.append(callout(f"{widest[0]:.0f}×", "Biggest spread",
                             f"{INFO_WL[w][1]}: {top['name']} vs {bot['name']}"))
    if evenest:
        _, w, top, bot = evenest
        cards.append(callout(f"{evenest[0]:.1f}×", "Closest race",
                             f"{INFO_WL[w][1]}: top to bottom"))
    callouts = '<div class="stats">' + "".join(cards) + "</div>"

    # One heatmap matrix per workload: a row per language (ranked by peak), a
    # column per worker count, the throughput in every cell, and the cell shaded
    # by its share of the best result at that worker count (darker = closer to
    # the column's winner, which is outlined). Keeps the full thread-count
    # detail while still reading at a glance.
    CLOSE = 0.97   # "close leader" = within 3% of the column's fastest result
    legend = ('<div class="hm-legend">'
              '<span class="lg lg-win">&#9733; 27.3M</span><span>fastest in column</span>'
              '<span class="lg lg-close">26.8M</span><span>within 3% of fastest</span>'
              '<span class="lg lg-low">&#9660; 158K</span><span>slowest in column</span>'
              '<span class="ramp"></span><span class="ramp-lab">shade = share of fastest</span>'
              '</div>')
    panels = []
    for w in (1, 2, 3, 4):
        if w not in workloads:
            continue
        m = workloads[w]
        icon, title, unit = INFO_WL[w]
        threads = m["threads"]
        ser = sorted((s for s in m["series"] if s["peak"] is not None),
                     key=lambda s: -s["peak"])
        # Best and worst value at each worker count (column).
        col_best, col_low = [], []
        for i in range(len(threads)):
            vals = [s["thr"][i] for s in ser if s["thr"][i] is not None]
            col_best.append(max(vals) if vals else None)
            col_low.append(min(vals) if vals else None)
        head = "".join(f'<th class="num">{t}</th>' for t in threads)
        body = ""
        for rank, s in enumerate(ser):
            medal = MEDALS.get(rank, f'<span class="rk">{rank+1}</span>')
            cells = ""
            for i, v in enumerate(s["thr"]):
                if v is None or not col_best[i]:
                    cells += '<td class="cell none">–</td>'
                    continue
                frac = v / col_best[i]
                alpha = 0.06 + 0.86 * frac ** 1.6   # perceptual-ish ramp
                ink = "#ffffff" if alpha > 0.52 else "var(--text-primary)"
                # Per-column call-outs: the leader (star + gold ring), close
                # leaders (thin gold ring), and the lowest score (red, marker).
                cls, txt = "", pf(v)
                if frac >= 0.999:
                    cls, txt = " win", "&#9733; " + txt
                elif frac >= CLOSE:
                    cls = " close"
                if v <= col_low[i] and len(ser) > 1 and frac < 0.999:
                    cls, txt = " low", "&#9660; " + txt
                    ink = "var(--low-ink)"
                cells += (f'<td class="cell{cls}" style="background:rgba(42,120,214,{alpha:.2f});'
                          f'color:{ink}">{txt}</td>')
            body += (f'<tr><td class="rank">{medal}</td>'
                     f'<td class="lang"><span class="dot" style="background:var(--cat-{SLOT.get(s["lang"],1)})"></span>{s["name"]}</td>'
                     f'{cells}<td class="peak">{pf(s["peak"])}</td></tr>')
        panels.append(
            f'<section class="panel"><div class="phead"><span class="icon">{icon}</span>'
            f'<div><h2>{title}</h2><div class="unit">{unit} &middot; rows ranked by best result</div></div></div>'
            f'<table class="hm"><thead><tr><th></th><th class="langh">Language</th>'
            f'<th class="grp" colspan="{len(threads)}">concurrent workers</th><th class="peakh">best</th></tr>'
            f'<tr><th></th><th></th>{head}<th></th></tr></thead>'
            f'<tbody>{body}</tbody></table></section>')

    return callouts + legend + '<div class="stack">' + "".join(panels) + "</div>"


def main():
    latest = load_rows()
    workloads = {}
    for w in (1, 2, 3, 4):
        m = build_workload(latest, w)
        if m:
            workloads[w] = m
    if not workloads:
        print("no v3 workload results found under results/*/", file=sys.stderr)
        sys.exit(1)

    langs = sorted({s["lang"] for w in workloads.values() for s in w["series"]})

    line_sections = ""
    bar_sections = ""
    ann_sections = ""
    rank_sections = ""
    png_line = []   # sid
    png_bars = []   # (sid, n_default_series, n_optimized_series, n_groups)
    png_ann = []    # sid (annotated line charts, taller)
    png_rank = []   # sid (ranked bar charts)
    for w in (1, 2, 3, 4):
        if w not in workloads:
            continue
        m = workloads[w]
        sid = f"w{w}"
        png_line.append(sid)
        png_line.append(f"{sid}-cpu")
        n_def = len([s for s in m["series"] if s["variant"] != "tuned"])
        n_opt = len([s for s in m["series"] if s["lang"] in ("go", "csharp")])
        png_bars.append((f"{sid}-bars", n_def, n_opt, len(m["threads"])))
        tabs = (f'<details class="tables" id="tables-{sid}" data-workload="{w}">'
                '<summary>Table view — throughput (' + m["unit"]
                + ')</summary><div class="tablewrap">'
                + table(m["series"], m["threads"]) + '</div></details>')
        line_sections += f"""
  <section id="sec-{sid}">
    <h2>{m['title']}</h2>
    <p class="sub">{m['sub']}</p>
    <div class="legend" id="legend-{sid}"></div>
    <div class="bigchart" id="chart-{sid}"></div>
    {tabs}
  </section>
  <section id="sec-{sid}-cpu">
    <h2>{m['title']} — client CPU cost</h2>
    <p class="sub">Client-side CPU consumed per operation (the benchmark process, user+sys), in microseconds of CPU per {'row' if w in (1,2) else 'blob'}, one line per language. Lower is cheaper. The MySQL server's own CPU is excluded — it is essentially the same per transaction across languages, so this isolates each language/driver's own CPU cost.</p>
    <div class="legend" id="legend-{sid}-cpu"></div>
    <div class="bigchart" id="chart-{sid}-cpu"></div>
  </section>"""
        png_rank.append((f"{sid}-rank", len(m["threads"])))
        rank_sections += f"""
  <section id="sec-{sid}-rank">
    <h2>{m['title']} — ranked</h2>
    <p class="sub">One column per worker count. In each column the languages are overlapping bars ({m['unit']}) of equal width, tallest first: each next-slower language is drawn on top and stepped a few pixels to the right, so the height difference between neighbours stays visible as a staircase (where neighbours are within a couple of pixels, bar heights are nudged apart slightly; the labels show the true values). Bars are coloured by language type &mdash; native, managed VM, or interpreted/scripting (legend at top). Beside the bars the languages are listed by rank, evenly spaced, each joined to its bar top by a thin right-angled connector. All columns share one scale.</p>
    <div class="bigchart" id="chart-{sid}-rank"></div>
  </section>"""
        png_ann.append(f"{sid}-ann")
        ann_sections += f"""
  <section id="sec-{sid}-ann">
    <h2>{m['title']} — every point labelled</h2>
    <p class="sub">Same throughput curves ({m['unit']}) as the main chart, but every data point carries its language and score, written in the gap to the right of the node and level with its value. Where points sit too close together the labels are spread apart and a thin line points from each node to its label.</p>
    <div class="legend" id="legend-{sid}-ann"></div>
    <div class="bigchart" id="chart-{sid}-ann"></div>
  </section>"""
        bar_sections += f"""
  <section id="sec-{sid}-bars">
    <h2>{m['title']} — grouped by worker count</h2>
    <p class="sub">Throughput ({m['unit']}) per language, grouped by worker count (512 at top down to 4), bars sorted best&nbsp;&rarr;&nbsp;worst within each group and coloured green&nbsp;(fastest)&nbsp;&rarr;&nbsp;red&nbsp;(slowest). A group whose slowest bar is &gt;50% below its fastest spans the full green&ndash;red range by rank; a tighter group shifts hue only as far as each bar's shortfall from the group&rsquo;s best.</p>
    <div class="legend" id="legend-{sid}-bars"></div>
    <div class="bigchart" id="chart-{sid}-bars"></div>
  </section>"""

    data_json = json.dumps({"workloads": workloads}, separators=(",", ":"))

    def render_page(out_path, sections, page_bars, title, h1, nav):
        html = HTML_TEMPLATE
        for k, v in {
            "__DATA__": data_json,
            "__SECTIONS__": sections,
            "__NLANG__": str(len(langs)),
            "__PAGE_BARS__": "true" if page_bars else "false",
            "__TITLE__": title,
            "__H1__": h1,
            "__NAV__": nav,
            "__CAT_LIGHT__": cat_css(PAL_LIGHT),
            "__CAT_DARK__": cat_css(PAL_DARK),
        }.items():
            html = html.replace(k, v)
        out_path.write_text(html)
        print(f"written: {out_path}")

    render_page(
        OUT, line_sections, False,
        "MySQL client benchmark — four micro-workloads by language",
        "MySQL client benchmark — four micro-workloads by language",
        '<a class="navlink" href="infographic.html">Summary &rarr;</a>'
        '<a class="navlink" href="annotated.html">Annotated &rarr;</a>'
        '<a class="navlink" href="ranked.html">Ranked &rarr;</a>'
        '<a class="navlink" href="bars.html">Bar charts &rarr;</a>')
    render_page(
        RANK_OUT, rank_sections, False,
        "MySQL client benchmark — ranked bars",
        "MySQL client benchmark — languages ranked by best throughput",
        '<a class="navlink" href="report.html">&larr; Line charts &amp; tables</a>'
        '<a class="navlink" href="bars.html">Bar charts &rarr;</a>')
    render_page(
        BARS_OUT, bar_sections, True,
        "MySQL client benchmark — bar charts by worker count",
        "MySQL client benchmark — bar charts by worker count",
        '<a class="navlink" href="report.html">&larr; Line charts &amp; tables</a>')
    render_page(
        ANN_OUT, ann_sections, False,
        "MySQL client benchmark — annotated line charts",
        "MySQL client benchmark — every data point labelled",
        '<a class="navlink" href="report.html">&larr; Line charts &amp; tables</a>'
        '<a class="navlink" href="bars.html">Bar charts &rarr;</a>')

    # Infographic summary page.
    info_html = INFOGRAPHIC_TEMPLATE
    for k, v in {
        "__BODY__": build_infographic(workloads),
        "__NLANG__": str(len(langs)),
        "__CAT_LIGHT__": cat_css(PAL_LIGHT),
        "__CAT_DARK__": cat_css(PAL_DARK),
    }.items():
        info_html = info_html.replace(k, v)
    INFO_OUT.write_text(info_html)
    print(f"written: {INFO_OUT}")
    print(f"workloads: {', '.join(map(str, workloads))}; {len(langs)} languages")

    if os.environ.get("BENCH_NO_PNG") == "1":
        return
    chromium = None
    for c in ("chromium", "chromium-browser", "google-chrome", "/snap/bin/chromium"):
        chromium = shutil.which(c) or (c if Path(c).exists() else None)
        if chromium:
            break
    if not chromium:
        print("chromium not found - skipping PNG export", file=sys.stderr)
        return
    PNG_DIR.mkdir(exist_ok=True)

    def shoot(png, url, h, w=PNG_W):
        cmd = [chromium, "--headless", "--disable-gpu", "--no-sandbox",
               "--hide-scrollbars", f"--force-device-scale-factor={PNG_SCALE}",
               f"--screenshot={png}", f"--window-size={w},{h}", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"png TIMEOUT for {png.name}", file=sys.stderr); return
        print(f"png: {png}" if png.exists()
              else f"png FAILED {png.name}: {r.stderr.strip()[-200:]}",
              file=sys.stderr if not png.exists() else sys.stdout)

    def bar_px_h(nseries, ngroups):
        # Mirror barChart's SVG geometry, scale to the PNG render width, and add
        # the section chrome (heading, subtitle, legend) so the bars aren't cropped.
        band = nseries * 13 + max(0, nseries - 1) * 2
        bih = ngroups * band + max(0, ngroups - 1) * 18
        bh_svg = 34 + bih + 30
        svg_px = RENDER_W * bh_svg / 1120
        return int(round(svg_px + 250))

    # Line charts: SVG aspect (viewBox 1120x540) scaled to the render width, plus
    # section chrome (heading, subtitle, legend). The CPU charts have a longer
    # subtitle, so they need extra vertical room to avoid cropping the bottom.
    svg_px = RENDER_W * 540 / 1120
    line_h = int(round(svg_px + 200))
    cpu_h = int(round(svg_px + 300))
    for sid in png_line:
        shoot(PNG_DIR / f"{sid}.png", f"file://{OUT}#png-{sid}",
              cpu_h if sid.endswith("-cpu") else line_h)
    # Bar charts: height sized to the content (one row per language x worker-count
    # groups) so the bottom (4-worker) group is never cropped.
    for sid, n_def, n_opt, ng in png_bars:
        shoot(PNG_DIR / f"{sid}.png", f"file://{BARS_OUT}#png-{sid}", bar_px_h(n_def, ng))
    # Ranked table charts: 1400-wide viewBox, 4 worker-count columns per row,
    # 456px per row (mirrors rankChart's TITLE_H+PLOT_H+ROW_B); exported at 1400px.
    RANK_W = 1400
    for sid, nthreads in png_rank:
        rows = -(-nthreads // 4)
        bh = 22 + rows * 456
        shoot(PNG_DIR / f"{sid}.png", f"file://{RANK_OUT}#png-{sid}",
              int(round((RANK_W - PNG_PAD) * bh / 1400 + 230)), RANK_W)
    # Annotated line charts: wider and taller SVG (viewBox 1400x840) so per-node
    # labels fit; exported at a wider PNG so the extra width is real pixels.
    ANN_W = 1400
    ann_h = int(round((ANN_W - PNG_PAD) * 840 / 1400 + 230))
    for sid in png_ann:
        shoot(PNG_DIR / f"{sid}.png", f"file://{ANN_OUT}#png-{sid}", ann_h, ANN_W)
    # Infographic summary: full static page — header + stats + legend, then one
    # stacked heatmap panel per workload (a 27px row per language + chrome).
    info_h = 420 + sum(len(m["series"]) * 27 + 118 for m in workloads.values())
    shoot(PNG_DIR / "infographic.png", f"file://{INFO_OUT}", info_h)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --band: #898781;
    --border: rgba(11,11,11,0.10);
    --card-shadow: inset 0 1px 0 rgba(255,255,255,0.7), 0 1px 2px rgba(17,17,17,0.04), 0 14px 32px -12px rgba(17,17,17,0.16);
    __CAT_LIGHT__
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --band: #898781;
      --border: rgba(255,255,255,0.10);
      --card-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.5), 0 16px 34px -12px rgba(0,0,0,0.7);
      __CAT_DARK__
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --band: #898781;
    --border: rgba(255,255,255,0.10);
    --card-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.5), 0 16px 34px -12px rgba(0,0,0,0.7);
    __CAT_DARK__
  }
  * { box-sizing: border-box; }
  body.viz-root { margin: 0; background: var(--page); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.45; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 64px; }
  header.page { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 21px; margin: 0; }
  .context { color: var(--text-secondary); margin: 6px 0 0; max-width: 72ch; }
  .controls { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  #theme-toggle { border: 1px solid var(--border); background: var(--surface-1);
    color: var(--text-secondary); border-radius: 8px; padding: 6px 12px; cursor: pointer; font: inherit; font-size: 13px; }
  section { margin-top: 42px; }
  h2 { font-size: 16.5px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 3px; }
  .sub { color: var(--text-secondary); margin: 0 0 14px; max-width: 80ch; }
  .legend { display: flex; gap: 14px 18px; align-items: center; margin: 0 0 12px; color: var(--text-secondary); font-size: 13px; flex-wrap: wrap; }
  .legend .item { display: inline-flex; align-items: center; gap: 6px; }
  .legend svg, #tooltip svg { display: inline-block; vertical-align: middle; }
  .legend .perfscale { width: 150px; height: 11px; border-radius: 6px;
    background: linear-gradient(90deg, hsl(120,66%,45%), hsl(60,66%,47%), hsl(0,66%,47%)); }
  .bigchart { background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px 10px; box-shadow: var(--card-shadow); }
  .bigchart svg { display: block; width: 100%; height: auto; }
  .bigchart svg:focus { outline: 2px solid var(--cat-1); outline-offset: 2px; border-radius: 4px; }
  svg text { font-family: inherit; font-size: 10.5px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  svg .seriesname { fill: var(--text-secondary); font-weight: 600; font-size: 11.5px; }
  svg .grouplabel { fill: var(--text-secondary); font-weight: 700; font-size: 11.5px; }
  svg .barval { fill: var(--muted); font-size: 9.5px; }
  svg .barvalin { fill: #ffffff; font-size: 9.5px; font-weight: 600; }
  svg .nodelabel { font-size: 11px; font-weight: 650; }
  svg .coltitle { fill: var(--text-primary); font-size: 13px; font-weight: 700; }
  svg .ranklab { font-size: 10.5px; font-weight: 650; }
  #tooltip { position: fixed; pointer-events: none; z-index: 10; display: none;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.18); padding: 8px 11px; font-size: 12.5px; min-width: 150px; }
  #tooltip .tt-title { color: var(--muted); margin-bottom: 4px; }
  #tooltip .tt-row { display: flex; align-items: center; gap: 7px; margin-top: 2px; }
  #tooltip .tt-val { font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }
  #tooltip .tt-name { color: var(--text-secondary); }
  details.tables { margin-top: 16px; }
  details.tables summary { cursor: pointer; color: var(--text-secondary); }
  .tablewrap { overflow-x: auto; margin-top: 10px; }
  table { border-collapse: collapse; background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; font-size: 13px; min-width: 640px; }
  th, td { padding: 6px 12px; text-align: left; border-bottom: 1px solid var(--grid); }
  thead th { color: var(--text-secondary); font-weight: 600; }
  tbody tr:last-child th, tbody tr:last-child td { border-bottom: none; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .navlink { color: var(--cat-1); text-decoration: none; font-size: 13px; font-weight: 600; align-self: center; }
  .navlink:hover { text-decoration: underline; }
  footer { color: var(--muted); font-size: 12px; margin-top: 30px; max-width: 86ch; }
  body.pngmode header.page, body.pngmode footer, body.pngmode section { display: none; }
  body.pngmode section.png-show { display: block; margin-top: 0; }
  body.pngmode section.png-show details.tables { display: none; }
  body.pngmode .wrap { padding: 16px 20px; max-width: none; }
</style>
</head>
<body class="viz-root">
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <filter id="chart-shadow" x="-10%" y="-20%" width="120%" height="140%">
    <feDropShadow dx="0" dy="0.8" stdDeviation="1.1" flood-color="#000" flood-opacity="0.18"/>
  </filter>
</defs></svg>
<div class="wrap">
  <header class="page">
    <div>
      <h1>__H1__</h1>
      <p class="context">Per worker (work scales with worker count): bulk-insert single-int
      rows in 10-row prepared batches, fetch ints and sum in-app, insert 1 MiB blobs with
      in-app MD5, read blobs with in-app MD5. __NLANG__ languages, Percona Server 8.4.11 tuned for maximum
      write throughput (no per-commit disk sync), client and server on one 80-core host,
      workers 4&nbsp;&rarr;&nbsp;512, no pre-warming. All languages run on stock runtime
      defaults.</p>
    </div>
    <div class="controls">
      __NAV__
      <button id="theme-toggle" type="button">Toggle dark mode</button>
    </div>
  </header>

__SECTIONS__
  <footer>
    Per-worker counts (work scales with worker count); barrier-synchronized timing;
    inserts run before the matching reads at each worker count; reads hit server-cached
    data (no warm-up). Client CPU = user+sys of the benchmark process tree; server CPU =
    mysqld utime+stime delta. Go and C# are run twice (stock and tuned runtime) and kept
    as separate benchmarks. Sources: <code>results/&lt;host&gt;/*.json</code>,
    <code>results/summary.csv</code>. Regenerate with
    <code>python3 report/generate_html_report.py</code>.
  </footer>
</div>

<div id="tooltip" role="status"></div>

<script>
const DATA = __DATA__;
const PAGE_BARS = __PAGE_BARS__;
(function () {
  "use strict";
  const pngMatch = location.hash.match(/^#png-(.+)$/);
  if (pngMatch) {
    document.documentElement.setAttribute("data-theme", "light");
    document.body.classList.add("pngmode");
    const sec = document.getElementById("sec-" + pngMatch[1]);
    if (sec) sec.classList.add("png-show");
  }

  // One curve per language (the tuned/optimized Go and C# runs were removed).
  const showSeries = s => s.variant !== "tuned";
  const labelOf = s => s.name;
  const dashOf = () => false;

  const tooltip = document.getElementById("tooltip");
  const fmt = v => v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
    : v >= 10000 ? Math.round(v / 1000) + "K" : Math.round(v).toLocaleString("en-US");
  const fmtFull = v => Math.round(v).toLocaleString("en-US");
  // Two-decimal compact form (27.23M / 87.42K / 339.70) so near-ties stay ordered.
  const fmt2 = v => v >= 1e6 ? (v / 1e6).toFixed(2) + "M"
    : v >= 1e3 ? (v / 1e3).toFixed(2) + "K" : v.toFixed(2);
  // CPU cost (us/op) spans ~0.1 to ~1e5; keep a couple of significant digits.
  const fmtCpu = v => v >= 10000 ? Math.round(v/1000) + "k"
    : v >= 100 ? Math.round(v).toLocaleString("en-US")
    : v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2);
  const ns = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => { const e = document.createElementNS(ns, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

  const STYLE = {
    c:{slot:1,shape:"circle"}, cpp:{slot:2,shape:"square"}, swift:{slot:3,shape:"triangle"},
    rust:{slot:8,shape:"diamond"}, php:{slot:5,shape:"tridown"}, java:{slot:6,shape:"plus"},
    csharp:{slot:11,shape:"star"}, go:{slot:10,shape:"diamond"}, javascript:{slot:4,shape:"tridown"},
    typescript:{slot:9,shape:"star"}, python:{slot:7,shape:"plus"}, ruby:{slot:12,shape:"triangle"},
    lua:{slot:13,shape:"x"},
  };
  const styleFor = lang => { const s = STYLE[lang] || {slot:1,shape:"circle"};
    return { color:`var(--cat-${s.slot})`, shape:s.shape }; };

  // Every marker is a filled circle in the series color (with a surface ring so
  // overlapping markers stay legible), carrying a small WHITE inner glyph that
  // identifies the language (circle / square / triangle / diamond / tridown /
  // plus / x / star). Returns a <g> of the circle plus the inner glyph.
  function shapeNode(shape, x, y, color, surface, ring = 2) {
    const g = el("g", {});
    g.appendChild(el("circle", { cx: x, cy: y, r: 6, fill: color, stroke: surface, "stroke-width": ring }));
    const w = "#ffffff", s = 2.5;
    const fill = { fill: w };
    const strk = { stroke: w, "stroke-width": 1.5, "stroke-linecap": "round", fill: "none" };
    let inner;
    if (shape === "square") inner = el("rect", { x: x-s+0.3, y: y-s+0.3, width: 2*s-0.6, height: 2*s-0.6, ...fill });
    else if (shape === "triangle") inner = el("path", { d: `M ${x} ${y-s-0.3} L ${x+s+0.1} ${y+s-0.2} L ${x-s-0.1} ${y+s-0.2} Z`, ...fill });
    else if (shape === "tridown") inner = el("path", { d: `M ${x} ${y+s+0.3} L ${x+s+0.1} ${y-s+0.2} L ${x-s-0.1} ${y-s+0.2} Z`, ...fill });
    else if (shape === "diamond") inner = el("path", { d: `M ${x} ${y-s-0.4} L ${x+s+0.4} ${y} L ${x} ${y+s+0.4} L ${x-s-0.4} ${y} Z`, ...fill });
    else if (shape === "plus") inner = el("path", { d: `M ${x} ${y-s-0.4} V ${y+s+0.4} M ${x-s-0.4} ${y} H ${x+s+0.4}`, ...strk });
    else if (shape === "x") { const d=s-0.1; inner = el("path", { d: `M ${x-d} ${y-d} L ${x+d} ${y+d} M ${x-d} ${y+d} L ${x+d} ${y-d}`, ...strk }); }
    else if (shape === "star") { const ro=s+0.6,ri=1.2,pts=[];
      for (let k=0;k<10;k++){ const r=k%2===0?ro:ri, ang=-Math.PI/2+k*Math.PI/5;
        pts.push((x+r*Math.cos(ang)).toFixed(2)+" "+(y+r*Math.sin(ang)).toFixed(2)); }
      inner = el("path", { d: "M "+pts.join(" L ")+" Z", ...fill }); }
    else inner = el("circle", { cx: x, cy: y, r: 2, ...fill }); // circle (default)
    g.appendChild(inner);
    return g;
  }
  function keySVG(st, w, h) {
    const svg = el("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    const ln = el("line", { x1: 1, x2: w-1, y1: h/2, y2: h/2, stroke: st.color, "stroke-width": 2, "stroke-linecap": "round" });
    if (st.dash) ln.setAttribute("stroke-dasharray", "5 4");
    svg.appendChild(ln); svg.appendChild(shapeNode(st.shape, w/2, h/2, st.color, "var(--surface-1)"));
    return svg;
  }
  function legendInto(hostId, entries) {
    const leg = document.getElementById(hostId); if (!leg) return;
    for (const e of entries) { const item = document.createElement("span"); item.className = "item";
      item.appendChild(e.key); item.appendChild(document.createTextNode(e.name)); leg.appendChild(item); }
  }
  // Bars are coloured by in-group performance, so the bar legend is a green->red
  // scale rather than a per-language key (languages are named at each bar).
  function perfLegend(hostId) {
    const leg = document.getElementById(hostId); if (!leg) return;
    const a = document.createElement("span"); a.className = "item"; a.textContent = "Fastest in group";
    const b = document.createElement("span"); b.className = "item"; b.textContent = "Slowest";
    const bar = document.createElement("span"); bar.className = "perfscale";
    leg.append(a, bar, b);
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1,1.5,2,2.5,3,4,5,6,8,10]) if (p*m >= v) return p*m;
    return p*10;
  }
  // A "nice" gridline interval that splits `range` into roughly `ticks` steps.
  function niceStep(range, ticks) {
    if (range <= 0) return 1;
    const raw = range/ticks, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw/p;
    return (f<=1?1:f<=2?2:f<=2.5?2.5:f<=5?5:10)*p;
  }

  function bigChart(cfg) {
    const T = cfg.T, N = T.length;
    const FY = cfg.fmtY || fmt;   // y-axis + end-label number format
    const BW = cfg.width || 1120, BH = cfg.height || 540, BM = { l: 64, r: 184, t: 14, b: 32 };
    const BIW = BW-BM.l-BM.r, BIH = BH-BM.t-BM.b;
    const X = i => BM.l + i*BIW/(N-1);
    const Y = v => BM.t + BIH - (v/cfg.yMax)*BIH;
    const svg = el("svg", { viewBox: `0 0 ${BW} ${BH}`, tabindex: "0", role: "img", "aria-label": cfg.ariaLabel });
    const step = cfg.yMax/5;
    for (let k=0;k<=5;k++){ const tv=step*k;
      svg.appendChild(el("line", { x1: BM.l, x2: BW-BM.r, y1: Y(tv), y2: Y(tv), stroke: "var(--grid)", "stroke-width": 1 }));
      const t = el("text", { x: BM.l-6, y: Y(tv)+3.5, "text-anchor": "end" }); t.textContent = tv===0?"0":FY(tv); svg.appendChild(t); }
    // Vertical gridline at each worker-count position.
    for (let i=0;i<N;i++) svg.appendChild(el("line", { x1: X(i), x2: X(i), y1: BM.t, y2: BM.t+BIH, stroke: "var(--grid)", "stroke-width": 1 }));
    svg.appendChild(el("line", { x1: BM.l, x2: BW-BM.r, y1: BM.t+BIH, y2: BM.t+BIH, stroke: "var(--baseline)", "stroke-width": 1 }));
    for (let i=0;i<N;i++){ const t = el("text", { x: X(i), y: BH-12, "text-anchor": "middle" }); t.textContent = T[i]; svg.appendChild(t); }
    // Optional shaded range band (e.g. server-CPU min..max across languages),
    // drawn behind the series lines.
    if (cfg.band) {
      const lo = cfg.band.lo, hi = cfg.band.hi, idxs = [];
      for (let i=0;i<N;i++) if (lo[i]!=null && hi[i]!=null) idxs.push(i);
      if (idxs.length >= 2) {
        let d = "";
        idxs.forEach((i,k) => { d += (k?"L":"M")+X(i).toFixed(1)+" "+Y(hi[i]).toFixed(1)+" "; });
        for (let k=idxs.length-1;k>=0;k--){ const i=idxs[k]; d += "L"+X(i).toFixed(1)+" "+Y(lo[i]).toFixed(1)+" "; }
        d += "Z";
        svg.appendChild(el("path", { d: d.trim(), fill: "var(--band)", "fill-opacity": "0.22", stroke: "none" }));
      }
    }
    const path = vals => { let d="",pen=false;
      for (let i=0;i<N;i++){ if (vals[i]==null){pen=false;continue;}
        d += (pen?"L":"M")+X(i).toFixed(1)+" "+Y(vals[i]).toFixed(1)+" "; pen=true; } return d.trim(); };
    const lastIdxOf = vals => { for (let i=N-1;i>=0;i--) if (vals[i]!=null) return i; return N-1; };
    // Series lines and markers are separate layers (both with a soft drop shadow
    // so they lift off the grid). On annotated charts, full-height translucent
    // panels go BETWEEN them: they dim the lines in every gap between worker
    // counts while the markers (nodes) and labels stay crisp on top.
    const lines = el("g", { filter: "url(#chart-shadow)" });
    const marks = el("g", { filter: "url(#chart-shadow)" });
    for (let si=cfg.seriesDefs.length-1; si>=0; si--) {
      const d = cfg.seriesDefs[si];
      const lp = el("path", { d: path(d.vals), fill: "none", stroke: d.st.color, "stroke-width": 2.4, "stroke-linejoin": "round", "stroke-linecap": "round" });
      if (d.st.dash) lp.setAttribute("stroke-dasharray", "7 5");
      lines.appendChild(lp);
      for (let i=0;i<N;i++){ if (d.vals[i]==null) continue; marks.appendChild(shapeNode(d.st.shape, X(i), Y(d.vals[i]), d.st.color, "var(--surface-1)", cfg.nodeLabels ? 0.8 : 2)); }
    }
    svg.appendChild(lines);
    if (cfg.nodeLabels) {
      // One translucent panel over the whole plot: lines beneath it are dimmed,
      // markers and labels are drawn on top of it.
      svg.appendChild(el("rect", { x: BM.l, y: BM.t, width: BIW, height: BIH, rx: 4, ry: 4,
                                   fill: "var(--surface-1)", "fill-opacity": "0.74",
                                   stroke: "var(--baseline)", "stroke-width": 1 }));
    }
    svg.appendChild(marks);
    // Per-node annotations: in the horizontal gap to the right of each worker
    // count, write "<language> <score>" level with that node's Y. Labels that
    // would overlap are pushed apart (min GAPN px) and kept inside the plot; a
    // label that had to move away from its node gets a thin leader line from
    // the node to the label. The last node keeps the end-label treatment below.
    if (cfg.nodeLabels) {
      const GAPN = 58;     // vertical spacing between stacked annotations
      for (let i = 0; i < N-1; i++) {
        const items = cfg.seriesDefs.filter(d => d.vals[i] != null)
          .map(d => ({ d, y0: Y(d.vals[i]), y: Y(d.vals[i]) }));
        items.sort((a, b) => a.y - b.y);
        for (let k = 1; k < items.length; k++) if (items[k].y - items[k-1].y < GAPN) items[k].y = items[k-1].y + GAPN;
        const over = items.length ? items[items.length-1].y - (BM.t + BIH - 2) : 0;
        if (over > 0) for (const L of items) L.y -= over;
        const topOver = items.length ? (BM.t + 5) - items[0].y : 0;
        if (topOver > 0) { items[0].y += topOver;
          for (let k = 1; k < items.length; k++) if (items[k].y - items[k-1].y < GAPN) items[k].y = items[k-1].y + GAPN; }
        // Label text starts well clear of the node so the leader is visible.
        const lx = X(i) + 34;
        for (const L of items) {
          if (Math.abs(L.y - L.y0) > 2.5) {
            const ldr = el("line", { x1: X(i)+7, y1: L.y0, x2: lx-5, y2: L.y, "stroke-width": 1, "stroke-opacity": "0.7" });
            ldr.style.stroke = L.d.st.color; svg.appendChild(ldr);
          }
          const t = el("text", { x: lx, y: L.y + 3.3, class: "nodelabel" });
          t.style.fill = L.d.st.color;
          t.textContent = L.d.name + " " + FY(L.d.vals[i]);
          svg.appendChild(t);
        }
      }
    }
    const labels = cfg.seriesDefs.map(d => { const li = lastIdxOf(d.vals); const yy = Y(d.vals[li]); return { d, li, endY: yy, y: yy }; });
    labels.sort((a,b)=>a.y-b.y);
    const GAPL = 16;
    for (let k=1;k<labels.length;k++) if (labels[k].y-labels[k-1].y < GAPL) labels[k].y = labels[k-1].y + GAPL;
    const over = labels.length ? labels[labels.length-1].y - (BM.t+BIH+6) : 0;
    if (over > 0) for (const L of labels) L.y -= over*(L.y-BM.t)/BIH;
    for (const L of labels) {
      const ax = X(L.li);
      if (Math.abs(L.y-L.endY) > 3 || ax < X(N-1)-1) {
        const ldr = el("line", { x1: ax+7, y1: L.endY, x2: X(N-1)+22, y2: L.y, "stroke-width": 1 });
        ldr.style.stroke = L.d.st.color; ldr.setAttribute("stroke-opacity", "0.6");
        svg.appendChild(ldr);
      }
      // End-of-line reference label (language + score) in the series' own colour.
      const t = el("text", { x: X(N-1)+26, y: L.y+3.5, class: "seriesname" });
      t.style.fill = L.d.st.color;
      t.textContent = L.d.name + "  " + FY(L.d.vals[L.li]); svg.appendChild(t);
    }
    const cross = el("line", { y1: BM.t, y2: BM.t+BIH, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
    svg.appendChild(cross);
    let idx = N-1;
    function show(i, cx, cy) {
      idx = i; cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
      tooltip.replaceChildren();
      const title = document.createElement("div"); title.className = "tt-title"; title.textContent = T[i] + " workers"; tooltip.appendChild(title);
      const ranked = cfg.seriesDefs.slice().sort((a,b)=> (b.vals[i]||0)-(a.vals[i]||0));
      for (const d of ranked) { const row = document.createElement("div"); row.className = "tt-row";
        row.appendChild(keySVG(d.st, 20, 11));
        const val = document.createElement("span"); val.className = "tt-val";
        val.textContent = d.vals[i]==null ? "n/a" : (cfg.fmtY ? FY(d.vals[i]) : fmtFull(d.vals[i])) + " " + cfg.unit;
        const name = document.createElement("span"); name.className = "tt-name"; name.textContent = d.name;
        row.append(val, name); tooltip.appendChild(row); }
      tooltip.style.display = "block";
      const tw = tooltip.offsetWidth, th = tooltip.offsetHeight, pad = 14;
      let left = cx + pad; if (left+tw > window.innerWidth-8) left = cx-tw-pad;
      tooltip.style.left = left + "px"; tooltip.style.top = Math.max(8, Math.min(cy-10, window.innerHeight-th-8)) + "px";
    }
    function hide(){ cross.setAttribute("visibility","hidden"); tooltip.style.display="none"; }
    svg.addEventListener("pointermove", ev => { const rect = svg.getBoundingClientRect();
      const px = (ev.clientX-rect.left)*(BW/rect.width); let best=0,bd=Infinity;
      for (let i=0;i<N;i++){ const d=Math.abs(px-X(i)); if (d<bd){bd=d;best=i;} } show(best, ev.clientX, ev.clientY); });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", () => { const r = svg.getBoundingClientRect(); show(idx, r.left+(X(idx)*r.width/BW), r.top+r.height/3); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", ev => { if (ev.key==="ArrowRight"||ev.key==="ArrowLeft"){ ev.preventDefault();
        const i = Math.min(N-1, Math.max(0, idx + (ev.key==="ArrowRight"?1:-1))); const r = svg.getBoundingClientRect();
        show(i, r.left+(X(i)*r.width/BW), r.top+r.height/3); } else if (ev.key==="Escape") hide(); });
    const host = document.getElementById(cfg.hostId); if (host) host.appendChild(svg);
  }

  // --- Shared tooltip helpers for the per-mark bar hover layer. ---
  function moveTip(ev) {
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight, pad = 14;
    let left = ev.clientX + pad; if (left+tw > window.innerWidth-8) left = ev.clientX-tw-pad;
    tooltip.style.left = left + "px";
    tooltip.style.top = Math.max(8, Math.min(ev.clientY-10, window.innerHeight-th-8)) + "px";
  }
  function hideTip() { tooltip.style.display = "none"; }
  function showBar(ev, workers, d, v, unit) {
    tooltip.replaceChildren();
    const title = document.createElement("div"); title.className = "tt-title";
    title.textContent = workers + " workers"; tooltip.appendChild(title);
    const row = document.createElement("div"); row.className = "tt-row";
    row.appendChild(keySVG(d.st, 20, 11));
    const val = document.createElement("span"); val.className = "tt-val";
    val.textContent = fmtFull(v) + " " + unit;
    const name = document.createElement("span"); name.className = "tt-name"; name.textContent = d.name;
    row.append(val, name); tooltip.appendChild(row);
    tooltip.style.display = "block"; moveTip(ev);
  }

  // Ranked vertical bars: items sorted best->worst left->right. Height encodes
  // the value and width encodes the rank (best = widest), so the best performer
  // is the tallest AND widest bar. Labels sit on the right, evenly spaced
  // vertically; each bar top connects to its label with a thin right-angled
  // connector (horizontal -> vertical -> horizontal). Crossings are avoided by
  // routing connectors through two channel groups: bars whose top is ABOVE
  // their label ("down" connectors) get channels ordered best-rightmost, bars
  // whose top is BELOW their label ("up" connectors) get channels ordered
  // best-leftmost. Within a group that ordering is crossing-free, and a down
  // connector can never cross an up connector, so no two connectors intersect.
  function rankChart(cfg) {
    // Table of worker-count columns. In each column: a nested bar group where
    // the bars are the LANGUAGES at that worker count (best = outer, tallest,
    // widest; each next one narrower and centred inside), then the languages
    // listed by rank, evenly spaced, each joined to its bar top by a thin
    // right-angled connector. Connectors never cross (see note above). One
    // shared y-scale per workload; columns wrap 4 per row.
    const items = cfg.items, S = items.length, T = cfg.T, NT = T.length;
    const COLS = 4, ROWS = Math.ceil(NT / COLS);
    const BW = 1400, ML = 58, MR = 14, CW = (BW - ML - MR) / COLS;
    const TITLE_H = 26, PLOT_H = 400, ROW_B = 30, RH = TITLE_H + PLOT_H + ROW_B, TOP = 22;  // TOP leaves room for the type legend
    const BH = TOP + ROWS * RH;
    const OFF = 6, BARW = 3 * OFF, CHW = 5;   // bar width = 3x the step; each shorter bar shifted right by OFF
    const svg = el("svg", { viewBox: `0 0 ${BW} ${BH}`, role: "img", "aria-label": cfg.ariaLabel });
    // Soft shadow a shorter bar casts leftward onto the taller bar beneath it.
    const shId = `rk-sh-${cfg.hostId}`;
    { const defs = el("defs", {});
      const g = el("linearGradient", { id: shId, x1: "0", y1: "0", x2: "1", y2: "0" });
      const s0 = el("stop", { offset: "0", "stop-color": "#000", "stop-opacity": "0" });
      const s1 = el("stop", { offset: "1", "stop-color": "#000", "stop-opacity": "0.22" });
      g.append(s0, s1); defs.appendChild(g); svg.appendChild(defs); }
    // Three colours by language type: native (compiled to machine code),
    // managed (bytecode on a VM with a JIT), interpreted/scripting.
    const LTYPE = { c: "native", cpp: "native", rust: "native", go: "native", swift: "native",
                    java: "managed", csharp: "managed",
                    python: "interp", php: "interp", ruby: "interp", lua: "interp", javascript: "interp", typescript: "interp" };
    const TCOL = { native: "var(--cat-1)", managed: "var(--cat-2)", interp: "var(--cat-3)" };
    const TNAME = { native: "native (C, C++, Rust, Go, Swift)", managed: "managed VM (Java, C#)", interp: "interpreted / scripting (Python, PHP, Ruby, Lua, JS, TS)" };
    const typeCol = it => TCOL[LTYPE[it.lang] || "interp"];
    // Legend along the top.
    { let lx = ML; for (const k of ["native", "managed", "interp"]) {
        svg.appendChild(el("rect", { x: lx, y: 0, width: 14, height: 11, fill: TCOL[k] }));
        const t = el("text", { x: lx + 18, y: 9.5 }); t.textContent = TNAME[k]; svg.appendChild(t);
        lx += 18 + TNAME[k].length * 5.6 + 22; } }
    const step = cfg.yMax / 5;
    T.forEach((t, ti) => {
      const c = ti % COLS, r = Math.floor(ti / COLS);
      const x0 = ML + c * CW, y0 = TOP + r * RH;
      const pt = y0 + TITLE_H, pb = pt + PLOT_H;              // plot top / bottom (baseline)
      const Y = v => pb - (v / cfg.yMax) * PLOT_H;
      const bx0 = x0 + 10;                                     // bar group left
      // Languages at this worker count, best first.
      const ranked = items.filter(it => it.vals[ti] != null).map(it => ({ it, v: it.vals[ti] })).sort((a, b) => b.v - a.v);
      const n = ranked.length, GW = BARW + Math.max(0, n - 1) * OFF;
      // Column title.
      const tt = el("text", { x: bx0, y: y0 + 17, class: "coltitle" }); tt.textContent = `${t} workers`; svg.appendChild(tt);
      // Gridlines behind the bar group; y tick labels once per row (first column).
      for (let k = 0; k <= 5; k++) { const tv = step * k;
        svg.appendChild(el("line", { x1: bx0 - 6, x2: bx0 + GW + 6, y1: Y(tv), y2: Y(tv), stroke: "var(--grid)", "stroke-width": 1 }));
        if (c === 0) { const yl = el("text", { x: ML - 6, y: Y(tv) + 3.5, "text-anchor": "end" }); yl.textContent = tv === 0 ? "0" : fmt(tv); svg.appendChild(yl); } }
      svg.appendChild(el("line", { x1: bx0 - 6, x2: bx0 + GW + 6, y1: pb, y2: pb, stroke: "var(--baseline)", "stroke-width": 1 }));
      // Flat equal-width bars, tallest first; each shorter one drawn on top,
      // shifted OFF px right, so the height differences read as a staircase.
      ranked.forEach(e => { e.col = typeCol(e.it); });
      // Visual nudge: neighbouring bars whose tops would be closer than MINGAP
      // px are pushed apart (shorter one down, cascading) so every step stays
      // visible; if the cascade would run past the baseline the whole stack is
      // lifted by the overflow. Labels and tooltips keep the true values.
      const MINGAP = 3;
      const yts = ranked.map(e => Y(e.v));
      for (let j = 1; j < yts.length; j++) if (yts[j] - yts[j-1] < MINGAP) yts[j] = yts[j-1] + MINGAP;
      const over = yts.length ? yts[yts.length-1] - (pb - 1) : 0;
      if (over > 0) for (let j = 0; j < yts.length; j++) yts[j] -= over;
      const tops = [];
      ranked.forEach((e, j) => {
        const yt = yts[j], h = Math.max(1, pb - yt);
        const w = BARW, bx = bx0 + j * OFF;
        if (j > 0) {   // shadow onto the taller bar to the left, along this bar's height
          svg.appendChild(el("rect", { x: bx - 4, y: yt, width: 4, height: h, fill: `url(#${shId})` }));
        }
        const bar = el("rect", { x: bx, y: yt, width: w, height: h, fill: e.col });
        bar.style.cursor = "crosshair";
        bar.addEventListener("pointerenter", ev => showBar(ev, t, e.it, e.v, cfg.unit));
        bar.addEventListener("pointermove", moveTip);
        bar.addEventListener("pointerleave", hideTip);
        svg.appendChild(bar);
        tops.push({ x: bx + w, y: yt });
      });
      // Ranked label list, evenly spaced over the plot height.
      const chan0 = bx0 + GW + 10, labelX = chan0 + n * CHW + 12;
      const ly = j => n > 1 ? pt + 6 + j * (PLOT_H - 12) / (n - 1) : pt + PLOT_H / 2;
      const down = [], up = [];
      ranked.forEach((_, j) => (tops[j].y <= ly(j) ? down : up).push(j));
      const chanX = {}; let slot = 0;
      for (const j of up) chanX[j] = chan0 + (slot++) * CHW;
      for (const j of down.slice().reverse()) chanX[j] = chan0 + (slot++) * CHW;
      ranked.forEach((e, j) => {
        const tp = tops[j], cxn = chanX[j], y2 = ly(j);
        const p = el("path", { d: `M ${tp.x.toFixed(1)} ${tp.y.toFixed(1)} H ${cxn.toFixed(1)} V ${y2.toFixed(1)} H ${(labelX - 5).toFixed(1)}`,
                                fill: "none", "stroke-width": 0.9, "stroke-linejoin": "miter", "stroke-opacity": "0.85" });
        p.style.stroke = e.col; svg.appendChild(p);
        const lab = el("text", { x: labelX, y: y2 + 3.5, class: "ranklab" });
        lab.style.fill = e.col; lab.textContent = `${j + 1}. ${e.it.name}  ${fmt2(e.v)}`; svg.appendChild(lab);
      });
    });
    const host = document.getElementById(cfg.hostId); if (host) host.appendChild(svg);
  }

  // Grouped horizontal bar chart: one group per worker count (512 at top down to
  // 4 at the bottom), one colored bar per language inside each group, throughput
  // along the x-axis with vertical gridlines and a value label at each bar tip.
  function barChart(cfg) {
    const T = cfg.T, N = T.length, S = cfg.seriesDefs.length;
    const barH = 13, barGap = 2, groupGap = 18;
    const bandH = S*barH + (S-1)*barGap;
    // Only a thin right margin: each bar's label sits in the empty space past
    // its own end, so no global gutter is reserved. The longest bars reach
    // ~97% of the plot width (axis max = peak/0.97), killing the dead space on
    // the right; those few whose label would overflow get an inside label.
    const BW = 1120, BM = { l: 78, r: 26, t: 34, b: 30 };
    const BIW = BW-BM.l-BM.r;
    const BIH = N*bandH + (N-1)*groupGap;
    const BH = BM.t + BIH + BM.b;
    const yMax = cfg.peak/0.97;
    const step = niceStep(yMax, 5);
    const X = v => BM.l + (v/yMax)*BIW;
    const svg = el("svg", { viewBox: `0 0 ${BW} ${BH}`, role: "img", "aria-label": cfg.ariaLabel });
    // Bars are coloured by performance within their group: green for the fastest
    // row, red for the slowest. Per-bar gradients (built below in the group loop)
    // run from a 50%-lighter tint at the bar start to the full hue at the tip.
    const gdefs = el("defs", {});
    svg.appendChild(gdefs);
    // hsl helpers: `h` in degrees (120=green .. 0=red). The left (start) stop is
    // a lighter tint of the full hue; both read on light and dark surfaces alike.
    const perfFill = h => `hsl(${h.toFixed(1)}, 66%, 45%)`;
    const perfTint = h => `hsl(${h.toFixed(1)}, 66%, 53%)`;
    // Throughput gridlines with tick labels along the top.
    for (let k=0; k*step<=yMax+1e-6; k++){ const tv=k*step, x=X(tv);
      svg.appendChild(el("line", { x1: x, x2: x, y1: BM.t, y2: BM.t+BIH, stroke: "var(--grid)", "stroke-width": 1 }));
      const t = el("text", { x: x, y: BM.t-8, "text-anchor": "middle" }); t.textContent = tv===0?"0":fmt(tv); svg.appendChild(t); }
    // Zero axis.
    svg.appendChild(el("line", { x1: BM.l, x2: BM.l, y1: BM.t, y2: BM.t+BIH, stroke: "var(--baseline)", "stroke-width": 1 }));
    // Groups, highest worker count on top down to lowest at the bottom.
    const order = T.map((_,i)=>i).reverse();
    const nn = v => v==null ? -Infinity : v;
    order.forEach((ti, gi) => {
      const bandTop = BM.t + gi*(bandH+groupGap);
      const gl = el("text", { x: BM.l-12, y: bandTop+bandH/2+3.5, "text-anchor": "end", class: "grouplabel" });
      gl.textContent = T[ti]; svg.appendChild(gl);
      // Within each group, order bars best-performing to worst (nulls last).
      let defs = cfg.seriesDefs;
      if (cfg.sortGroup) defs = cfg.seriesDefs.slice().sort((a,b) => nn(b.vals[ti]) - nn(a.vals[ti]));
      // Performance hue per group. If the slowest bar is >50% below the fastest
      // (a wide spread), map green->red by RANK across the rows. Otherwise the
      // group is tight, so map each bar's hue by how far its value sits below
      // the group max (reaching red only at a 50% deficit) — a gentler shift.
      const present = defs.map((d, idx) => ({ idx, v: d.vals[ti] }))
                          .filter(o => o.v != null).sort((a, b) => b.v - a.v);
      const K = present.length, gmax = K ? present[0].v : 0, gmin = K ? present[K-1].v : 0;
      const spread = gmax > 0 ? (gmax - gmin) / gmax : 0;
      const rankByValue = spread > 0.5;
      const rankOf = {}; present.forEach((o, rr) => { rankOf[o.idx] = rr; });
      const hueFor = (idx, v) => rankByValue
        ? 120 * (1 - (K > 1 ? rankOf[idx] / (K - 1) : 0))
        : 120 * (1 - Math.min(gmax > 0 ? (gmax - v) / gmax : 0, 0.5) / 0.5);
      defs.forEach((d, j) => {
        const v = d.vals[ti]; if (v==null) return;
        const by = bandTop + j*(barH+barGap);
        const w = Math.max(1, (v/yMax)*BIW);
        const r = Math.min(4, w, barH/2);
        const hue = hueFor(j, v), gid = `g-${cfg.hostId}-${ti}-${j}`;
        const grad = el("linearGradient", { id: gid, x1: "0", y1: "0", x2: "1", y2: "0" });
        const s0 = el("stop", { offset: "0" }); s0.style.stopColor = perfTint(hue);
        const s1 = el("stop", { offset: "1" }); s1.style.stopColor = perfFill(hue);
        grad.append(s0, s1); gdefs.appendChild(grad);
        const rect = el("rect", { x: BM.l, y: by, width: w, height: barH, rx: r, ry: r, fill: `url(#${gid})` });
        if (d.st.dash) rect.setAttribute("fill-opacity", "0.55");
        rect.style.cursor = "crosshair";
        rect.addEventListener("pointerenter", ev => showBar(ev, T[ti], d, v, cfg.unit));
        rect.addEventListener("pointermove", moveTip);
        rect.addEventListener("pointerleave", hideTip);
        svg.appendChild(rect);
        const txt = cfg.labelName ? fmt(v) + " - " + d.name : fmt(v);
        const endX = BM.l + w, ty = by + barH/2 + 3.5;
        const approxW = txt.length*5.7 + 4;
        let lab;
        if (endX + 5 + approxW <= BW - 4) {   // label fits to the right of the bar
          lab = el("text", { x: endX+5, y: ty, "text-anchor": "start", class: "barval" });
        } else {                              // very long bar: label inside, right-aligned (white via .barvalin)
          lab = el("text", { x: endX-6, y: ty, "text-anchor": "end", class: "barvalin" });
        }
        lab.textContent = txt; svg.appendChild(lab);
      });
    });
    const host = document.getElementById(cfg.hostId); if (host) host.appendChild(svg);
  }

  for (const w of Object.keys(DATA.workloads)) {
    const M = DATA.workloads[w];
    const shown = M.series.filter(showSeries);
    const stOf = s => ({ ...styleFor(s.lang), dash: dashOf(s) });
    const defs = shown.map(s => ({ name: labelOf(s), vals: s.thr, st: stOf(s) }));
    legendInto(`legend-w${w}`, shown.map(s => ({ name: labelOf(s), key: keySVG(stOf(s), 26, 12) })));
    const peak = Math.max(1, ...shown.map(s => Math.max(...s.thr.filter(v => v != null))));
    bigChart({
      hostId: `chart-w${w}`, T: M.threads, seriesDefs: defs,
      yMax: niceMax(peak), unit: M.unit,
      ariaLabel: `${M.title}: throughput by worker count`,
    });
    perfLegend(`legend-w${w}-bars`);
    barChart({
      hostId: `chart-w${w}-bars`, T: M.threads, seriesDefs: defs,
      peak: peak, unit: M.unit, sortGroup: PAGE_BARS, labelName: PAGE_BARS,
      ariaLabel: `${M.title}: throughput grouped by worker count`,
    });
    // Ranked bars (ranked.html): best = tallest + widest, connectors to labels.
    if (document.getElementById(`chart-w${w}-rank`)) {
      const items = shown.map(s => ({ name: labelOf(s), lang: s.lang, vals: s.thr, value: Math.max(...s.thr.filter(v => v != null)), st: stOf(s) }))
        .sort((a, b) => b.value - a.value);
      rankChart({ hostId: `chart-w${w}-rank`, items, T: M.threads, yMax: niceMax(peak), unit: M.unit,
                  ariaLabel: `${M.title}: languages ranked by best throughput, nested bars per worker count` });
    }
    // Annotated line chart (annotated.html): every node labelled in the gap.
    if (document.getElementById(`chart-w${w}-ann`)) {
      legendInto(`legend-w${w}-ann`, shown.map(s => ({ name: labelOf(s), key: keySVG(stOf(s), 26, 12) })));
      bigChart({
        hostId: `chart-w${w}-ann`, T: M.threads, seriesDefs: defs,
        yMax: niceMax(peak), unit: M.unit, nodeLabels: true, height: 840, width: 1400,
        ariaLabel: `${M.title}: throughput by worker count, every point labelled`,
      });
    }
    // Client CPU-cost line chart (us of benchmark-process CPU per operation).
    if (document.getElementById(`chart-w${w}-cpu`)) {
      const cdefs = shown.filter(s => s.cpu && s.cpu.some(v => v != null))
        .map(s => ({ name: labelOf(s), vals: s.cpu, st: stOf(s) }));
      legendInto(`legend-w${w}-cpu`, cdefs.map(d => ({ name: d.name, key: keySVG(d.st, 26, 12) })));
      const cpeak = Math.max(...cdefs.flatMap(d => d.vals.filter(v => v != null)), 0.001);
      bigChart({
        hostId: `chart-w${w}-cpu`, T: M.threads, seriesDefs: cdefs,
        yMax: niceMax(cpeak), unit: "µs client CPU/op", fmtY: fmtCpu,
        ariaLabel: `${M.title}: client CPU cost per operation by worker count`,
      });
    }
  }

  document.getElementById("theme-toggle").addEventListener("click", () => {
    const r = document.documentElement;
    const dark = r.getAttribute("data-theme") === "dark" ||
      (!r.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    r.setAttribute("data-theme", dark ? "light" : "dark");
  });

  // Deep-linking: ?workload=N focuses a workload's throughput section;
  // ?cpu=1 targets that workload's CPU-cost chart instead; ?table=1 also opens
  // its table view. Opening/closing a table updates the URL so it can be
  // copied/shared, and on load we scroll to the targeted place.
  const secIdFor = n => PAGE_BARS ? `sec-w${n}-bars` : `sec-w${n}`;
  if (!pngMatch) {
    const q = new URLSearchParams(location.search);
    const wl = parseInt(q.get("workload") || "", 10);
    const tableOn = q.get("table") === "1";
    const cpuOn = q.get("cpu") === "1";
    const tbl = (!PAGE_BARS && wl >= 1) ? document.getElementById(`tables-w${wl}`) : null;
    let target = null;
    if (tableOn && tbl) { tbl.open = true; target = tbl; }
    else if (cpuOn && wl >= 1 && !PAGE_BARS) target = document.getElementById(`sec-w${wl}-cpu`);
    else if (wl >= 1) target = document.getElementById(secIdFor(wl));
    if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }
  for (const d of document.querySelectorAll("details.tables")) {
    d.addEventListener("toggle", () => {
      const q = new URLSearchParams(location.search);
      q.set("workload", d.getAttribute("data-workload"));
      q.set("table", d.open ? "1" : "0");
      history.replaceState(null, "", location.pathname + "?" + q.toString() + location.hash);
    });
  }
})();
</script>
</body>
</html>
"""


INFOGRAPHIC_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySQL client benchmark — at a glance</title>
<style>
  .viz-root { color-scheme: light;
    --surface-1: #fcfcfb; --page: #f6f6f3; --text-primary: #0b0b0b;
    --text-secondary: #52514e; --muted: #898781; --grid: #e7e6df;
    --border: rgba(11,11,11,0.10);
    --card-shadow: inset 0 1px 0 rgba(255,255,255,0.7), 0 1px 2px rgba(17,17,17,0.04), 0 14px 32px -12px rgba(17,17,17,0.16);
    --gold: #e8a800; --gold-soft: rgba(232,168,0,0.55);
    --low-ink: #c0262b; --low-ring: rgba(192,38,43,0.45);
    __CAT_LIGHT__ }
  @media (prefers-color-scheme: dark) { .viz-root {
    color-scheme: dark; --surface-1: #1a1a19; --page: #0d0d0d; --text-primary: #f0efec;
    --text-secondary: #c3c2b7; --muted: #898781; --grid: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --card-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.5), 0 16px 34px -12px rgba(0,0,0,0.7);
    --gold: #f2c23a; --gold-soft: rgba(242,194,58,0.6);
    --low-ink: #ff7a7a; --low-ring: rgba(255,122,122,0.5);
    __CAT_DARK__ } }
  * { box-sizing: border-box; }
  body.viz-root { margin: 0; background: var(--page); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 30px 24px 48px; }
  header { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 25px; margin: 0; letter-spacing: -0.02em; }
  .context { color: var(--text-secondary); margin: 7px 0 0; max-width: 70ch; }
  .navlink { color: var(--cat-1); text-decoration: none; font-weight: 600; font-size: 14px; align-self: center; }
  .navlink:hover { text-decoration: underline; }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 22px 0 6px; }
  .stat { background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px;
    padding: 15px 18px; box-shadow: var(--card-shadow); }
  .stat-big { font-size: 30px; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1; }
  .stat-label { font-size: 13px; color: var(--text-secondary); font-weight: 650; margin-top: 4px; }
  .stat-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .hm-legend { display: flex; align-items: center; gap: 8px 10px; flex-wrap: wrap; color: var(--text-secondary);
    font-size: 12.5px; margin: 14px 0 4px; }
  .hm-legend > span:nth-child(even) { margin-right: 10px; }
  .lg { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 12px; font-weight: 650;
    font-variant-numeric: tabular-nums; }
  .lg-win { background: rgba(42,120,214,0.92); color: #fff; box-shadow: inset 0 0 0 2.5px var(--gold); }
  .lg-close { background: rgba(42,120,214,0.72); color: #fff; box-shadow: inset 0 0 0 2px var(--gold-soft); }
  .lg-low { background: rgba(42,120,214,0.06); color: var(--low-ink); box-shadow: inset 0 0 0 1.5px var(--low-ring); }
  .ramp { width: 110px; height: 10px; border-radius: 5px; margin-left: 6px;
    background: linear-gradient(90deg, rgba(42,120,214,0.06), rgba(42,120,214,0.92)); }
  .ramp-lab { color: var(--muted); }
  .stack { display: flex; flex-direction: column; gap: 16px; margin-top: 12px; }
  .panel { background: var(--surface-1); border: 1px solid var(--border); border-radius: 16px;
    padding: 16px 18px 14px; box-shadow: var(--card-shadow); }
  .phead { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .icon { font-size: 25px; line-height: 1; }
  .panel h2 { font-size: 16.5px; margin: 0; font-weight: 650; letter-spacing: -0.01em; }
  .unit { font-size: 12px; color: var(--muted); margin-top: 1px; }
  table.hm { width: 100%; border-collapse: separate; border-spacing: 3px 3px; font-size: 12.5px; }
  table.hm th { font-weight: 600; color: var(--muted); font-size: 11.5px; padding: 0 2px 2px; text-align: center; }
  table.hm th.langh { text-align: left; padding-left: 4px; }
  table.hm th.grp { color: var(--text-secondary); letter-spacing: 0.02em; text-transform: uppercase; font-size: 10.5px; }
  table.hm th.peakh { color: var(--text-secondary); }
  table.hm td { padding: 0; height: 24px; }
  td.rank { width: 24px; text-align: center; font-size: 14px; }
  .rk { display: inline-block; width: 18px; height: 18px; line-height: 18px; border-radius: 50%;
    background: var(--grid); color: var(--text-secondary); font-size: 11px; font-weight: 600; }
  td.lang { width: 104px; color: var(--text-secondary); font-weight: 600; white-space: nowrap; padding-left: 4px; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
  td.cell { text-align: center; border-radius: 5px; font-variant-numeric: tabular-nums; font-weight: 550; }
  td.cell.none { color: var(--muted); }
  td.cell.win { font-weight: 750; box-shadow: inset 0 0 0 2.5px var(--gold); }
  td.cell.close { font-weight: 650; box-shadow: inset 0 0 0 2px var(--gold-soft); }
  td.cell.low { font-weight: 700; box-shadow: inset 0 0 0 1.5px var(--low-ring); }
  td.peak { width: 56px; text-align: right; padding-right: 4px; font-weight: 700; font-variant-numeric: tabular-nums; }
  footer { color: var(--muted); font-size: 12px; margin-top: 24px; max-width: 86ch; }
  @media (max-width: 760px) { .stats { grid-template-columns: 1fr; } table.hm { font-size: 11px; } }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header>
    <div>
      <h1>MySQL from __NLANG__ languages &mdash; at a glance</h1>
      <p class="context">Every language&rsquo;s throughput for every task at every worker count (3-run average). Each row is a language, each column a concurrency level; darker cells are closer to the fastest result in that column. Full charts on the <a class="navlink" href="report.html">charts page</a>.</p>
    </div>
    <a class="navlink" href="report.html">Full charts &rarr;</a>
  </header>
  __BODY__
  <footer>Per-worker workloads, barrier-timed, Percona Server 8.4.11 tuned for write throughput (no per-commit fsync), workers 4&nbsp;&rarr;&nbsp;512 on 80-core hosts. &ldquo;Best per language&rdquo; is the peak across worker counts. Bars are coloured by language (same colours as the charts).</footer>
</div>
</body>
</html>
"""

if __name__ == "__main__":
    main()
