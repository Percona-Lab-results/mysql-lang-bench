#!/usr/bin/env python3
"""Generate report/report.html plus light-mode PNGs of every chart.

Reads mode-tagged results (latest per (mode, lang, threads)) and renders,
per benchmark mode (work-based "events" and fixed-duration "duration"):
  - a TPS-by-worker-count chart
  - a client/server CPU-per-transaction chart
with legends, hover layer, dark mode, and table views. After writing the
HTML it screenshots each chart section with headless Chromium into
report/png/<section>.png (light mode, 2x scale). Set BENCH_NO_PNG=1 to skip.
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
    "typescript": "TypeScript", "python": "Python", "ruby": "Ruby",
    "lua": "Lua",
}
MODE_TITLE = {"events": "Work-based mode", "duration": "Fixed-duration mode"}


def load_rows():
    latest = {}
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "threads" not in r or "client_cpu_s" not in r or "mode" not in r:
            continue
        # variant: True (tuned runtime), False (stock runtime), or None (n/a).
        # Go and C# carry both a stock and a tuned run; keep them as separate
        # series so the before/after of the runtime tuning is visible.
        variant = r.get("tuned")
        latest[(r["mode"], r["lang"], variant, r["threads"])] = r
    return latest


def build_mode(latest, mode):
    threads = sorted({t for (m, _, _, t) in latest if m == mode})
    groups = sorted({(l, v) for (m, l, v, _) in latest if m == mode})
    if not threads or not groups:
        return None
    series = []
    dur = 0
    for lang, variant in groups:
        tps, cli, srv = [], [], []
        drv = ""
        for t in threads:
            r = latest.get((mode, lang, variant, t))
            if r is None:
                tps.append(None); cli.append(None); srv.append(None)
                continue
            tps.append(round(r["tps"], 1))
            cli.append(round(r["client_cpu_s"] / r["txns"] * 1e6))
            srv.append(round(r["server_cpu_s"] / r["txns"] * 1e6))
            drv = f'{r.get("driver", "")} {r.get("driver_version", "")}'.strip()
            dur = r.get("duration_s", 0) or dur
        if any(v is not None for v in tps):
            stock = variant is False
            series.append({
                "lang": lang,
                "name": DISPLAY.get(lang, lang) + (" (stock)" if stock else ""),
                "driver": drv, "dash": stock,
                "tps": tps, "cli": cli, "srv": srv,
                "peak": max(v for v in tps if v is not None),
            })
    if not series:
        return None
    series.sort(key=lambda s: -s["peak"])
    n = len(threads)
    srv_lo = [min((s["srv"][i] for s in series if s["srv"][i] is not None), default=0) for i in range(n)]
    srv_hi = [max((s["srv"][i] for s in series if s["srv"][i] is not None), default=0) for i in range(n)]
    title = MODE_TITLE.get(mode, mode) + (
        f" — {dur}s per rung" if mode == "duration" else " — 10,000 txns per worker")
    return {"threads": threads, "series": series, "srvLo": srv_lo,
            "srvHi": srv_hi, "title": title}


def table(series, threads, key, fmt):
    head = "".join(f'<th class="num">{t}</th>' for t in threads)
    body = ""
    for s in series:
        cells = "".join(
            f'<td class="num">{fmt(v) if v is not None else "–"}</td>' for v in s[key])
        body += f'<tr><th scope="row">{s["name"]}</th>{cells}</tr>\n'
    return (f'<table><thead><tr><th>Language</th>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


def main():
    latest = load_rows()
    modes = {}
    for mode in ("events",):  # work-based only; fixed-duration dropped
        m = build_mode(latest, mode)
        if m:
            modes[mode] = m
    if not modes:
        print("no mode-tagged CPU-instrumented results found", file=sys.stderr)
        sys.exit(1)

    # KPI row from the primary mode (events if present)
    pm = modes.get("events") or next(iter(modes.values()))
    best = pm["series"][0]
    peak_t = pm["threads"][best["tps"].index(best["peak"])]
    cheapest = min(pm["series"], key=lambda s: min(v for v in s["cli"] if v is not None))
    cheap_v = min(v for v in cheapest["cli"] if v is not None)
    dearest = max(pm["series"], key=lambda s: max(v for v in s["cli"] if v is not None))
    dear_v = max(v for v in dearest["cli"] if v is not None)
    kpis = [
        ("Peak throughput", f"{best['peak']:,.0f} tps", f"{best['name']} at {peak_t} threads"),
        ("Cheapest client", f"{cheap_v:,} µs/txn", f"{cheapest['name']}, CPU per transaction"),
        ("Costliest client", f"{dear_v:,} µs/txn", f"{dearest['name']}"),
        ("Languages", str(len({s["lang"] for s in pm["series"]})), "same workload, same server, per-language data load"),
    ]
    kpi_html = "\n".join(
        f'<div class="tile"><div class="tile-label">{l}</div>'
        f'<div class="tile-value">{v}</div><div class="tile-sub">{s}</div></div>'
        for l, v, s in kpis)

    sections = ""
    png_ids = []
    for metric, mtitle, msub in (
        ("tps", "Throughput by worker count",
         "Transactions per second. Each language keeps its own color and marker shape; "
         "lines are named at their right end. Hover for every language&rsquo;s value."),
        ("cpu", "CPU cost per transaction",
         "Client CPU microseconds per committed transaction; the gray band is the server "
         "(mysqld) cost across all languages, min to max, on the same axis."),
    ):
        for mode, m in modes.items():
            sid = f"{metric}-{mode}"
            png_ids.append(sid)
            if metric == "tps":
                tabs = ('<details class="tables"><summary>Table view — transactions per second</summary>'
                        f'<div class="tablewrap">{table(m["series"], m["threads"], "tps", lambda v: f"{v:,.0f}")}</div></details>')
            else:
                tabs = ('<details class="tables"><summary>Table view — client CPU µs per transaction</summary>'
                        f'<div class="tablewrap">{table(m["series"], m["threads"], "cli", lambda v: f"{v:,}")}</div></details>'
                        '<details class="tables"><summary>Table view — server CPU µs per transaction</summary>'
                        f'<div class="tablewrap">{table(m["series"], m["threads"], "srv", lambda v: f"{v:,}")}</div></details>')
            sections += f"""
  <section id="sec-{sid}">
    <h2>{mtitle} — {m['title']}</h2>
    <p class="sub">{msub}</p>
    <div class="legend" id="legend-{sid}"></div>
    <div class="bigchart" id="chart-{sid}"></div>
    {tabs}
  </section>"""

    html = HTML_TEMPLATE
    for k, v in {
        "__DATA__": json.dumps({"modes": modes}, separators=(",", ":")),
        "__KPIS__": kpi_html,
        "__SECTIONS__": sections,
    }.items():
        html = html.replace(k, v)
    OUT.write_text(html)
    print(f"written: {OUT} (modes: {', '.join(modes)}; {len(pm['series'])} languages)")

    # ---- PNG export (light mode) ----
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
    for sid in png_ids:
        png = PNG_DIR / f"{sid}.png"
        cmd = [chromium, "--headless", "--disable-gpu", "--no-sandbox",
               "--hide-scrollbars", "--force-device-scale-factor=2",
               f"--screenshot={png}", "--window-size=1280,780",
               f"file://{OUT}#png-{sid}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"png TIMEOUT for {sid}", file=sys.stderr)
            continue
        if png.exists():
            print(f"png: {png}")
        else:
            print(f"png FAILED for {sid}: {r.stderr.strip()[-200:]}", file=sys.stderr)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MySQL client benchmark — TPS &amp; CPU cost by language</title>
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
  body.viz-root {
    margin: 0; background: var(--page); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.45;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 64px; }
  header.page { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 21px; margin: 0; }
  .context { color: var(--text-secondary); margin: 6px 0 0; max-width: 72ch; }
  #theme-toggle {
    border: 1px solid var(--border); background: var(--surface-1);
    color: var(--text-secondary); border-radius: 8px; padding: 6px 12px;
    cursor: pointer; font: inherit; font-size: 13px;
  }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 22px 0 8px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .tile-label { color: var(--text-secondary); font-size: 13px; }
  .tile-value { font-size: 26px; font-weight: 600; margin-top: 2px; }
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
  #tooltip {
    position: fixed; pointer-events: none; z-index: 10; display: none;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.18); padding: 8px 11px; font-size: 12.5px; min-width: 150px;
  }
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
  /* PNG-export mode: show exactly one section, light theme, no chrome */
  body.pngmode header.page, body.pngmode .kpis, body.pngmode footer,
  body.pngmode section { display: none; }
  body.pngmode section.png-show { display: block; margin-top: 0; }
  body.pngmode section.png-show details.tables { display: none; }
  body.pngmode .wrap { padding: 16px 20px; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header class="page">
    <div>
      <h1>MySQL client benchmark — throughput &amp; CPU cost by language</h1>
      <p class="context">sysbench <code>oltp_read_write</code>-style transactions (10 point selects,
      4 range selects, 2 updates, delete+insert per txn), 10&nbsp;M-row table, Percona Server 8.4.11,
      one language per pristine data load, 15-minute read-only sysbench warm-up before each sweep,
      client and server on the same 80-core host. Workers: 4&nbsp;&rarr;&nbsp;512.
      Work-based workload: every worker runs a fixed 10,000 transactions per rung.
      Go and C# appear twice — a solid line for tuned runtime settings
      (Go <code>GOMAXPROCS=16 GOGC=800</code>; C# server GC) and a dashed
      <em>(stock)</em> line for the stock runtime defaults, so the tuning gain is
      visible. The other languages run on stock defaults.</p>
    </div>
    <button id="theme-toggle" type="button">Toggle dark mode</button>
  </header>

  <div class="kpis">__KPIS__</div>
__SECTIONS__
  <footer>
    Methodology: per-worker PRNG with identical streams across languages; warmup excluded from the
    timed window via a start barrier; retries on deadlock/lock-wait/duplicate-key with full redraw.
    Client CPU = user+sys of the whole benchmark process tree; server CPU = mysqld utime+stime delta
    over the same window. Sources: <code>results/&lt;host&gt;/*.json</code>,
    <code>results/summary.csv</code>. Regenerate (HTML + light-mode PNGs in
    <code>report/png/</code>) with <code>python3 report/generate_html_report.py</code>.
  </footer>
</div>

<div id="tooltip" role="status"></div>

<script>
const DATA = __DATA__;
(function () {
  "use strict";
  // PNG-export mode: #png-<section id> renders one section, light theme.
  const pngMatch = location.hash.match(/^#png-(.+)$/);
  if (pngMatch) {
    document.documentElement.setAttribute("data-theme", "light");
    document.body.classList.add("pngmode");
    const sec = document.getElementById("sec-" + pngMatch[1]);
    if (sec) sec.classList.add("png-show");
  }

  const tooltip = document.getElementById("tooltip");
  const fmt = v => v >= 10000 ? Math.round(v / 1000) + "K" : Math.round(v).toLocaleString("en-US");
  const fmtFull = v => Math.round(v).toLocaleString("en-US");
  const ns = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => {
    const e = document.createElementNS(ns, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  // Fixed per-language identity: unique hue + marker shape (circle/square used once).
  const STYLE = {
    c:          { slot: 1,  shape: "circle"   },
    cpp:        { slot: 2,  shape: "square"   },
    swift:      { slot: 3,  shape: "triangle" },
    rust:       { slot: 8,  shape: "diamond"  },
    php:        { slot: 5,  shape: "tridown"  },
    java:       { slot: 6,  shape: "plus"     },
    csharp:     { slot: 11, shape: "star"     },
    go:         { slot: 10, shape: "diamond"  },
    javascript: { slot: 4,  shape: "tridown"  },
    typescript: { slot: 9,  shape: "star"     },
    python:     { slot: 7,  shape: "plus"     },
    ruby:       { slot: 12, shape: "triangle" },
    lua:        { slot: 13, shape: "x"        },
  };
  const styleFor = lang => {
    const s = STYLE[lang] || { slot: 1, shape: "circle" };
    return { color: `var(--cat-${s.slot})`, shape: s.shape };
  };

  function shapeNode(shape, x, y, color, surface) {
    const common = { fill: color, stroke: surface, "stroke-width": 2, "stroke-linejoin": "round" };
    const s = 4.6;
    if (shape === "circle") return el("circle", { cx: x, cy: y, r: s, ...common });
    if (shape === "square") return el("rect", { x: x - s + 0.4, y: y - s + 0.4, width: 2 * s - 0.8, height: 2 * s - 0.8, rx: 1, ...common });
    if (shape === "triangle") return el("path", { d: `M ${x} ${y - s - 0.6} L ${x + s + 0.4} ${y + s - 0.4} L ${x - s - 0.4} ${y + s - 0.4} Z`, ...common });
    if (shape === "tridown") return el("path", { d: `M ${x} ${y + s + 0.6} L ${x + s + 0.4} ${y - s + 0.4} L ${x - s - 0.4} ${y - s + 0.4} Z`, ...common });
    if (shape === "plus") {
      const a = 1.9, p = s + 1;
      return el("path", { d: `M ${x - a} ${y - p} H ${x + a} V ${y - a} H ${x + p} V ${y + a} H ${x + a} V ${y + p} H ${x - a} V ${y + a} H ${x - p} V ${y - a} H ${x - a} Z`, ...common });
    }
    if (shape === "star") {
      const ro = s + 1.6, ri = 2.4, pts = [];
      for (let k = 0; k < 10; k++) {
        const r = k % 2 === 0 ? ro : ri;
        const ang = -Math.PI / 2 + k * Math.PI / 5;
        pts.push((x + r * Math.cos(ang)).toFixed(2) + " " + (y + r * Math.sin(ang)).toFixed(2));
      }
      return el("path", { d: "M " + pts.join(" L ") + " Z", ...common });
    }
    if (shape === "x") {
      const a = 1.5, p = s + 0.8, m = a * 1.42;
      return el("path", { d: `M ${x - p} ${y - p + m} L ${x - m} ${y - p} L ${x} ${y - m} L ${x + m} ${y - p} L ${x + p} ${y - p + m} L ${x + m} ${y} L ${x + p} ${y + p - m} L ${x + m} ${y + p} L ${x} ${y + m} L ${x - m} ${y + p} L ${x - p} ${y + p - m} L ${x - m} ${y} Z`, ...common });
    }
    return el("path", { d: `M ${x} ${y - s - 1} L ${x + s + 1} ${y} L ${x} ${y + s + 1} L ${x - s - 1} ${y} Z`, ...common }); // diamond
  }

  function keySVG(st, w, h) {
    const svg = el("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    const ln = el("line", { x1: 1, x2: w - 1, y1: h / 2, y2: h / 2, stroke: st.color, "stroke-width": 2, "stroke-linecap": "round" });
    if (st.dash) ln.setAttribute("stroke-dasharray", "5 4");
    svg.appendChild(ln);
    svg.appendChild(shapeNode(st.shape, w / 2, h / 2, st.color, "var(--surface-1)"));
    return svg;
  }

  function legendInto(hostId, entries) {
    const leg = document.getElementById(hostId);
    if (!leg) return;
    for (const e of entries) {
      const item = document.createElement("span");
      item.className = "item";
      item.appendChild(e.key);
      item.appendChild(document.createTextNode(e.name));
      leg.appendChild(item);
    }
  }

  function bigChart(cfg) {
    const T = cfg.T, N = T.length;
    const BW = 1120, BH = 540, BM = { l: 56, r: 170, t: 14, b: 32 };
    const BIW = BW - BM.l - BM.r, BIH = BH - BM.t - BM.b;
    const X = i => BM.l + i * BIW / (N - 1);
    const Y = v => BM.t + BIH - (v / cfg.yMax) * BIH;
    const svg = el("svg", { viewBox: `0 0 ${BW} ${BH}`, tabindex: "0", role: "img", "aria-label": cfg.ariaLabel });
    for (let tv = 0; tv <= cfg.yMax; tv += cfg.yStep) {
      svg.appendChild(el("line", { x1: BM.l, x2: BW - BM.r, y1: Y(tv), y2: Y(tv), stroke: "var(--grid)", "stroke-width": 1 }));
      const t = el("text", { x: BM.l - 6, y: Y(tv) + 3.5, "text-anchor": "end" });
      t.textContent = tv === 0 ? "0" : (cfg.yMax >= 10000 ? (tv / 1000) + "K" : tv.toLocaleString("en-US"));
      svg.appendChild(t);
    }
    svg.appendChild(el("line", { x1: BM.l, x2: BW - BM.r, y1: BM.t + BIH, y2: BM.t + BIH, stroke: "var(--baseline)", "stroke-width": 1 }));
    for (let i = 0; i < N; i++) {
      const t = el("text", { x: X(i), y: BH - 12, "text-anchor": "middle" });
      t.textContent = T[i];
      svg.appendChild(t);
    }
    if (cfg.band) {
      const up = cfg.band.hi.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
      const down = cfg.band.lo.map((_, i) => "L" + X(N - 1 - i).toFixed(1) + " " + Y(cfg.band.lo[N - 1 - i]).toFixed(1)).join(" ");
      svg.appendChild(el("path", { d: up + " " + down + " Z", fill: "var(--band)", opacity: 0.16, stroke: "none" }));
    }
    // null-aware: a missing rung breaks the line rather than dropping to 0.
    const path = vals => {
      let d = "", pen = false;
      for (let i = 0; i < N; i++) {
        if (vals[i] == null) { pen = false; continue; }
        d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(vals[i]).toFixed(1) + " ";
        pen = true;
      }
      return d.trim();
    };
    const lastIdxOf = vals => { for (let i = N - 1; i >= 0; i--) if (vals[i] != null) return i; return N - 1; };
    for (let si = cfg.seriesDefs.length - 1; si >= 0; si--) {
      const d = cfg.seriesDefs[si];
      const lp = el("path", { d: path(d.vals), fill: "none", stroke: d.st.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
      if (d.st.dash) lp.setAttribute("stroke-dasharray", "7 5");
      svg.appendChild(lp);
      for (let i = 0; i < N; i++) {
        if (d.vals[i] == null) continue;
        svg.appendChild(shapeNode(d.st.shape, X(i), Y(d.vals[i]), d.st.color, "var(--surface-1)"));
      }
    }
    const labels = cfg.seriesDefs.map(d => { const li = lastIdxOf(d.vals); const yy = Y(d.vals[li]); return { d, li, endY: yy, y: yy }; });
    if (cfg.band) labels.push({ band: true, li: N - 1, endY: Y((cfg.band.lo[N - 1] + cfg.band.hi[N - 1]) / 2), y: Y((cfg.band.lo[N - 1] + cfg.band.hi[N - 1]) / 2) });
    labels.sort((a, b) => a.y - b.y);
    const GAPL = 16;
    for (let k = 1; k < labels.length; k++) {
      if (labels[k].y - labels[k - 1].y < GAPL) labels[k].y = labels[k - 1].y + GAPL;
    }
    const over = labels[labels.length - 1].y - (BM.t + BIH + 6);
    if (over > 0) for (const L of labels) L.y -= over * (L.y - BM.t) / BIH;
    for (const L of labels) {
      const anchorX = L.band ? X(N - 1) : X(L.li);
      if (Math.abs(L.y - L.endY) > 3 || anchorX < X(N - 1) - 1) {
        svg.appendChild(el("line", { x1: anchorX + 7, y1: L.endY, x2: X(N - 1) + 22, y2: L.y, stroke: "var(--baseline)", "stroke-width": 1 }));
      }
      const t = el("text", { x: X(N - 1) + 26, y: L.y + 3.5, class: "seriesname" });
      t.textContent = L.band ? cfg.band.name : L.d.name + "  " + fmt(L.d.vals[L.li]);
      if (L.band) t.setAttribute("fill", "var(--muted)");
      svg.appendChild(t);
    }
    const cross = el("line", { y1: BM.t, y2: BM.t + BIH, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
    svg.appendChild(cross);
    let idx = N - 1;
    function show(i, cx, cy) {
      idx = i;
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i));
      cross.setAttribute("visibility", "visible");
      tooltip.replaceChildren();
      const title = document.createElement("div");
      title.className = "tt-title"; title.textContent = T[i] + " threads";
      tooltip.appendChild(title);
      const ranked = cfg.seriesDefs.slice().sort((a, b) => b.vals[i] - a.vals[i]);
      for (const d of ranked) {
        const row = document.createElement("div"); row.className = "tt-row";
        row.appendChild(keySVG(d.st, 20, 11));
        const val = document.createElement("span"); val.className = "tt-val";
        val.textContent = (d.vals[i] == null) ? "n/a" : fmtFull(d.vals[i]) + (cfg.unit ? " " + cfg.unit : "");
        const name = document.createElement("span"); name.className = "tt-name"; name.textContent = d.name;
        row.append(val, name); tooltip.appendChild(row);
      }
      if (cfg.band) {
        const row = document.createElement("div"); row.className = "tt-row";
        const sw = document.createElement("span");
        sw.style.cssText = "width:20px;height:9px;background:var(--band);opacity:.25;border-radius:2px;flex:none;";
        const val = document.createElement("span"); val.className = "tt-val";
        val.textContent = fmtFull(cfg.band.lo[i]) + "–" + fmtFull(cfg.band.hi[i]) + (cfg.unit ? " " + cfg.unit : "");
        const name = document.createElement("span"); name.className = "tt-name"; name.textContent = cfg.band.name;
        row.append(sw, val, name); tooltip.appendChild(row);
      }
      tooltip.style.display = "block";
      const tw = tooltip.offsetWidth, th = tooltip.offsetHeight, pad = 14;
      let left = cx + pad;
      if (left + tw > window.innerWidth - 8) left = cx - tw - pad;
      tooltip.style.left = left + "px";
      tooltip.style.top = Math.max(8, Math.min(cy - 10, window.innerHeight - th - 8)) + "px";
    }
    function hide() { cross.setAttribute("visibility", "hidden"); tooltip.style.display = "none"; }
    svg.addEventListener("pointermove", ev => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * (BW / rect.width);
      let best = 0, bd = Infinity;
      for (let i = 0; i < N; i++) { const d = Math.abs(px - X(i)); if (d < bd) { bd = d; best = i; } }
      show(best, ev.clientX, ev.clientY);
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", () => {
      const r = svg.getBoundingClientRect();
      show(idx, r.left + (X(idx) * r.width / BW), r.top + r.height / 3);
    });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", ev => {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        const i = Math.min(N - 1, Math.max(0, idx + (ev.key === "ArrowRight" ? 1 : -1)));
        const r = svg.getBoundingClientRect();
        show(i, r.left + (X(i) * r.width / BW), r.top + r.height / 3);
      } else if (ev.key === "Escape") hide();
    });
    const host = document.getElementById(cfg.hostId);
    if (host) host.appendChild(svg);
  }

  for (const mode of Object.keys(DATA.modes)) {
    const M = DATA.modes[mode];
    const stOf = s => ({ ...styleFor(s.lang), dash: s.dash });
    const defsFor = key => M.series.map(s => ({ name: s.name, vals: s[key], st: stOf(s) }));
    const langLegend = () => M.series.map(s => ({ name: s.name, key: keySVG(stOf(s), 26, 12) }));

    legendInto(`legend-tps-${mode}`, langLegend());
    bigChart({
      hostId: `chart-tps-${mode}`, T: M.threads, seriesDefs: defsFor("tps"),
      yMax: 30000, yStep: 5000, unit: "tps", band: null,
      ariaLabel: `Transactions per second by worker count, ${mode} mode`,
    });

    const cpuMax = Math.ceil(Math.max(...M.series.map(s => Math.max(...s.cli)), ...M.srvHi) / 2000) * 2000 || 2000;
    const bandKey = document.createElement("span");
    bandKey.style.cssText = "width:26px;height:10px;background:var(--band);opacity:.25;border-radius:2px;display:inline-block;";
    legendInto(`legend-cpu-${mode}`, langLegend().concat([{ name: "Server (mysqld), min–max", key: bandKey }]));
    bigChart({
      hostId: `chart-cpu-${mode}`, T: M.threads, seriesDefs: defsFor("cli"),
      yMax: cpuMax, yStep: cpuMax > 16000 ? 4000 : 2000, unit: "µs",
      band: { lo: M.srvLo, hi: M.srvHi, name: "Server (mysqld)" },
      ariaLabel: `Client CPU microseconds per transaction, ${mode} mode`,
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
