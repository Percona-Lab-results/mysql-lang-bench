#!/usr/bin/env python3
"""Generate report/report.html plus light-mode PNGs — v3 four-workload report.

One throughput-vs-worker-count line chart per workload (1..4). Go and C# carry
a stock and a tuned run; the "Show optimized Go/C#" toggle (URL ?optimized=0|1,
default 0) switches the graphs between all-languages/stock and a Go+C#-only
stock-vs-optimized focus, reloading on change. Each chart is also exported as a
light-mode PNG (default and optimized variants) into report/png/.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "report" / "report.html"
PNG_DIR = ROOT / "report" / "png"

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
        for t in threads:
            r = latest.get((w, lang, variant, t))
            thr.append(round(r["throughput"], 1) if r else None)
            if r:
                unit = r.get("unit", unit)
        if any(v is not None for v in thr):
            series.append({
                "lang": lang,
                "name": DISPLAY.get(lang, lang),
                "variant": "stock" if variant is False else ("tuned" if variant is True else "na"),
                "thr": thr,
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
    kpis = []
    for w in (1, 2, 3, 4):
        if w not in workloads:
            continue
        mv = [s for s in workloads[w]["series"] if s["variant"] in ("na", "stock")]
        best = max(mv, key=lambda s: s["peak"])
        unit = workloads[w]["unit"]
        val = f"{best['peak']/1e6:.2f}M" if best["peak"] >= 1e6 else f"{best['peak']:,.0f}"
        kpis.append((WL[w][0].split("—")[1].strip(), f"{val} {unit}",
                     f"best: {best['name']}"))
    kpi_html = "\n".join(
        f'<div class="tile"><div class="tile-label">{l}</div>'
        f'<div class="tile-value">{v}</div><div class="tile-sub">{s}</div></div>'
        for l, v, s in kpis)

    sections = ""
    png_ids = []
    for w in (1, 2, 3, 4):
        if w not in workloads:
            continue
        m = workloads[w]
        sid = f"w{w}"
        png_ids.append(sid)
        tabs = ('<details class="tables"><summary>Table view — throughput ('
                + m["unit"] + ')</summary><div class="tablewrap">'
                + table(m["series"], m["threads"]) + '</div></details>')
        sections += f"""
  <section id="sec-{sid}">
    <h2>{m['title']}</h2>
    <p class="sub">{m['sub']}</p>
    <div class="legend" id="legend-{sid}"></div>
    <div class="bigchart" id="chart-{sid}"></div>
    {tabs}
  </section>"""

    html = HTML_TEMPLATE
    for k, v in {
        "__DATA__": json.dumps({"workloads": workloads}, separators=(",", ":")),
        "__KPIS__": kpi_html,
        "__SECTIONS__": sections,
        "__NLANG__": str(len(langs)),
    }.items():
        html = html.replace(k, v)
    OUT.write_text(html)
    print(f"written: {OUT} (workloads: {', '.join(map(str, workloads))}; {len(langs)} languages)")

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

    def shoot(png, url):
        cmd = [chromium, "--headless", "--disable-gpu", "--no-sandbox",
               "--hide-scrollbars", "--force-device-scale-factor=2",
               f"--screenshot={png}", "--window-size=1280,760", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"png TIMEOUT for {png.name}", file=sys.stderr); return
        print(f"png: {png}" if png.exists()
              else f"png FAILED {png.name}: {r.stderr.strip()[-200:]}",
              file=sys.stderr if not png.exists() else sys.stdout)

    for sid in png_ids:
        shoot(PNG_DIR / f"{sid}.png", f"file://{OUT}#png-{sid}")
        shoot(PNG_DIR / f"{sid}-optimized.png", f"file://{OUT}?optimized=1#png-{sid}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySQL client benchmark — four micro-workloads by language</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --band: #898781;
    --border: rgba(11,11,11,0.10);
    --cat-1: #2a78d6; --cat-2: #eb6834; --cat-3: #1baf7a; --cat-4: #eda100;
    --cat-5: #e87ba4; --cat-6: #008300; --cat-7: #6250d6; --cat-8: #e34948;
    --cat-9: #84b000; --cat-10: #0092c4; --cat-11: #8c5a2b; --cat-12: #b5174e; --cat-13: #00847c;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --band: #898781;
      --border: rgba(255,255,255,0.10);
      --cat-1: #3987e5; --cat-2: #d95926; --cat-3: #199e70; --cat-4: #c98500;
      --cat-5: #d55181; --cat-6: #008300; --cat-7: #9085e9; --cat-8: #e66767;
      --cat-9: #a3c636; --cat-10: #35b5e0; --cat-11: #b07845; --cat-12: #d6447c; --cat-13: #2aaea5;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --band: #898781;
    --border: rgba(255,255,255,0.10);
    --cat-1: #3987e5; --cat-2: #d95926; --cat-3: #199e70; --cat-4: #c98500;
    --cat-5: #d55181; --cat-6: #008300; --cat-7: #9085e9; --cat-8: #e66767;
    --cat-9: #a3c636; --cat-10: #35b5e0; --cat-11: #b07845; --cat-12: #d6447c; --cat-13: #2aaea5;
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
  .toggle { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    border: 1px solid var(--border); background: var(--surface-1); color: var(--text-secondary);
    border-radius: 8px; padding: 6px 12px; font-size: 13px; }
  .toggle input { cursor: pointer; margin: 0; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 22px 0 8px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .tile-label { color: var(--text-secondary); font-size: 13px; }
  .tile-value { font-size: 23px; font-weight: 600; margin-top: 2px; }
  .tile-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  section { margin-top: 34px; }
  h2 { font-size: 16px; margin: 0 0 2px; }
  .sub { color: var(--text-secondary); margin: 0 0 14px; max-width: 80ch; }
  .legend { display: flex; gap: 16px; align-items: center; margin: 0 0 10px; color: var(--text-secondary); font-size: 13px; flex-wrap: wrap; }
  .legend .item { display: inline-flex; align-items: center; gap: 6px; }
  .legend svg, #tooltip svg { display: inline-block; vertical-align: middle; }
  .bigchart { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px 8px; }
  .bigchart svg { display: block; width: 100%; height: auto; }
  .bigchart svg:focus { outline: 2px solid var(--cat-1); outline-offset: 2px; border-radius: 4px; }
  svg text { font-family: inherit; font-size: 10.5px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  svg .seriesname { fill: var(--text-secondary); font-weight: 600; font-size: 11.5px; }
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
  footer { color: var(--muted); font-size: 12px; margin-top: 30px; max-width: 86ch; }
  body.pngmode header.page, body.pngmode .kpis, body.pngmode footer, body.pngmode section { display: none; }
  body.pngmode section.png-show { display: block; margin-top: 0; }
  body.pngmode section.png-show details.tables { display: none; }
  body.pngmode .wrap { padding: 16px 20px; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header class="page">
    <div>
      <h1>MySQL client benchmark — four micro-workloads by language</h1>
      <p class="context">Per worker (work scales with worker count): bulk-insert single-int
      rows in 10-row prepared batches, fetch ints and sum in-app, insert 1 MiB blobs with
      in-app MD5, read blobs with in-app MD5. __NLANG__ languages, Percona Server 8.4.11 tuned for maximum
      write throughput (no per-commit disk sync), client and server on one 80-core host,
      workers 4&nbsp;&rarr;&nbsp;512, no pre-warming. All languages run on stock runtime
      defaults; enable <em>Show optimized Go/C#</em> to focus on Go and C# with their
      stock and tuned-runtime curves.</p>
    </div>
    <div class="controls">
      <label class="toggle"><input type="checkbox" id="opt-toggle"> Show optimized Go/C#</label>
      <button id="theme-toggle" type="button">Toggle dark mode</button>
    </div>
  </header>

  <div class="kpis">__KPIS__</div>
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
(function () {
  "use strict";
  const pngMatch = location.hash.match(/^#png-(.+)$/);
  if (pngMatch) {
    document.documentElement.setAttribute("data-theme", "light");
    document.body.classList.add("pngmode");
    const sec = document.getElementById("sec-" + pngMatch[1]);
    if (sec) sec.classList.add("png-show");
  }

  const params = new URLSearchParams(location.search);
  const showOptimized = params.get("optimized") === "1";
  const optToggle = document.getElementById("opt-toggle");
  if (optToggle) {
    optToggle.checked = showOptimized;
    optToggle.addEventListener("change", () => {
      params.set("optimized", optToggle.checked ? "1" : "0");
      location.search = params.toString();
    });
  }
  // Off: all languages, stock curves. On: only Go and C#, stock + optimized.
  const showSeries = s => showOptimized
    ? (s.lang === "go" || s.lang === "csharp")
    : s.variant !== "tuned";
  const labelOf = s => s.variant === "tuned" ? s.name + " (optimized)"
    : (s.variant === "stock" && showOptimized) ? s.name + " (stock)" : s.name;
  const dashOf = s => s.variant === "tuned";

  const tooltip = document.getElementById("tooltip");
  const fmt = v => v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
    : v >= 10000 ? Math.round(v / 1000) + "K" : Math.round(v).toLocaleString("en-US");
  const fmtFull = v => Math.round(v).toLocaleString("en-US");
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
  function shapeNode(shape, x, y, color, surface) {
    const g = el("g", {});
    g.appendChild(el("circle", { cx: x, cy: y, r: 6, fill: color, stroke: surface, "stroke-width": 2 }));
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

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1,1.5,2,2.5,3,4,5,6,8,10]) if (p*m >= v) return p*m;
    return p*10;
  }

  function bigChart(cfg) {
    const T = cfg.T, N = T.length;
    const BW = 1120, BH = 540, BM = { l: 64, r: 184, t: 14, b: 32 };
    const BIW = BW-BM.l-BM.r, BIH = BH-BM.t-BM.b;
    const X = i => BM.l + i*BIW/(N-1);
    const Y = v => BM.t + BIH - (v/cfg.yMax)*BIH;
    const svg = el("svg", { viewBox: `0 0 ${BW} ${BH}`, tabindex: "0", role: "img", "aria-label": cfg.ariaLabel });
    const step = cfg.yMax/5;
    for (let k=0;k<=5;k++){ const tv=step*k;
      svg.appendChild(el("line", { x1: BM.l, x2: BW-BM.r, y1: Y(tv), y2: Y(tv), stroke: "var(--grid)", "stroke-width": 1 }));
      const t = el("text", { x: BM.l-6, y: Y(tv)+3.5, "text-anchor": "end" }); t.textContent = tv===0?"0":fmt(tv); svg.appendChild(t); }
    // Vertical gridline at each worker-count position.
    for (let i=0;i<N;i++) svg.appendChild(el("line", { x1: X(i), x2: X(i), y1: BM.t, y2: BM.t+BIH, stroke: "var(--grid)", "stroke-width": 1 }));
    svg.appendChild(el("line", { x1: BM.l, x2: BW-BM.r, y1: BM.t+BIH, y2: BM.t+BIH, stroke: "var(--baseline)", "stroke-width": 1 }));
    for (let i=0;i<N;i++){ const t = el("text", { x: X(i), y: BH-12, "text-anchor": "middle" }); t.textContent = T[i]; svg.appendChild(t); }
    const path = vals => { let d="",pen=false;
      for (let i=0;i<N;i++){ if (vals[i]==null){pen=false;continue;}
        d += (pen?"L":"M")+X(i).toFixed(1)+" "+Y(vals[i]).toFixed(1)+" "; pen=true; } return d.trim(); };
    const lastIdxOf = vals => { for (let i=N-1;i>=0;i--) if (vals[i]!=null) return i; return N-1; };
    for (let si=cfg.seriesDefs.length-1; si>=0; si--) {
      const d = cfg.seriesDefs[si];
      const lp = el("path", { d: path(d.vals), fill: "none", stroke: d.st.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
      if (d.st.dash) lp.setAttribute("stroke-dasharray", "7 5");
      svg.appendChild(lp);
      for (let i=0;i<N;i++){ if (d.vals[i]==null) continue; svg.appendChild(shapeNode(d.st.shape, X(i), Y(d.vals[i]), d.st.color, "var(--surface-1)")); }
    }
    const labels = cfg.seriesDefs.map(d => { const li = lastIdxOf(d.vals); const yy = Y(d.vals[li]); return { d, li, endY: yy, y: yy }; });
    labels.sort((a,b)=>a.y-b.y);
    const GAPL = 16;
    for (let k=1;k<labels.length;k++) if (labels[k].y-labels[k-1].y < GAPL) labels[k].y = labels[k-1].y + GAPL;
    const over = labels.length ? labels[labels.length-1].y - (BM.t+BIH+6) : 0;
    if (over > 0) for (const L of labels) L.y -= over*(L.y-BM.t)/BIH;
    for (const L of labels) {
      const ax = X(L.li);
      if (Math.abs(L.y-L.endY) > 3 || ax < X(N-1)-1)
        svg.appendChild(el("line", { x1: ax+7, y1: L.endY, x2: X(N-1)+22, y2: L.y, stroke: "var(--baseline)", "stroke-width": 1 }));
      const t = el("text", { x: X(N-1)+26, y: L.y+3.5, class: "seriesname" });
      t.textContent = L.d.name + "  " + fmt(L.d.vals[L.li]); svg.appendChild(t);
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
        val.textContent = d.vals[i]==null ? "n/a" : fmtFull(d.vals[i]) + " " + cfg.unit;
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
  }

  document.getElementById("theme-toggle").addEventListener("click", () => {
    const r = document.documentElement;
    const dark = r.getAttribute("data-theme") === "dark" ||
      (!r.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    r.setAttribute("data-theme", dark ? "light" : "dark");
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
