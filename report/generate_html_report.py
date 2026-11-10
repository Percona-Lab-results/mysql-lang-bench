#!/usr/bin/env python3
"""Generate report/report.html — self-contained HTML benchmark report.

Reads results/<host>/*.json (latest run per (lang, threads), v2 rows with CPU
fields), embeds the data as JSON, and renders:
  - KPI stat tiles
  - TPS vs threads: one large multi-series line chart (hue x shape x dash)
  - CPU cost per txn: one large chart (client lines + server min-max band)
  - Table views for all three matrices
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "report" / "report.html"

DISPLAY = {
    "c": "C", "cpp": "C++", "swift": "Swift", "rust": "Rust", "php": "PHP",
    "java": "Java", "csharp": "C#", "go": "Go", "javascript": "JavaScript",
    "typescript": "TypeScript", "python": "Python", "ruby": "Ruby",
}


def load_rows():
    latest = {}
    for f in sorted(ROOT.glob("results/*/*.json")):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "threads" not in r or "client_cpu_s" not in r:
            continue
        latest[(r["lang"], r["threads"])] = r
    return latest


def main():
    latest = load_rows()
    if not latest:
        print("no CPU-instrumented results found", file=sys.stderr)
        sys.exit(1)

    threads = sorted({t for (_, t) in latest})
    langs = sorted({l for (l, _) in latest})

    series = []
    for lang in langs:
        tps, cli, srv = [], [], []
        drv = ""
        for t in threads:
            r = latest.get((lang, t))
            if r is None:
                tps.append(None); cli.append(None); srv.append(None)
                continue
            tps.append(round(r["tps"], 1))
            cli.append(round(r["client_cpu_s"] / r["txns"] * 1e6))
            srv.append(round(r["server_cpu_s"] / r["txns"] * 1e6))
            drv = f'{r.get("driver", "")} {r.get("driver_version", "")}'.strip()
        series.append({
            "lang": lang, "name": DISPLAY.get(lang, lang), "driver": drv,
            "tps": tps, "cli": cli, "srv": srv,
            "peak": max(v for v in tps if v is not None),
        })
    series.sort(key=lambda s: -s["peak"])

    srv_lo = [min(s["srv"][i] for s in series) for i in range(len(threads))]
    srv_hi = [max(s["srv"][i] for s in series) for i in range(len(threads))]

    best = series[0]
    peak_t = threads[best["tps"].index(best["peak"])]
    cheapest = min(series, key=lambda s: min(v for v in s["cli"] if v is not None))
    cheap_v = min(v for v in cheapest["cli"] if v is not None)
    dearest = max(series, key=lambda s: max(v for v in s["cli"] if v is not None))
    dear_v = max(v for v in dearest["cli"] if v is not None)
    srv_low = round(sum(s["srv"][0] for s in series) / len(series))

    data = {"threads": threads, "series": series, "srvLo": srv_lo, "srvHi": srv_hi}
    kpis = [
        ("Peak throughput", f"{best['peak']:,.0f} tps", f"{best['name']} at {peak_t} threads"),
        ("Server CPU per txn", f"{srv_low / 1000:.1f} ms", "at 4 threads, near-uniform across languages"),
        ("Cheapest client", f"{cheap_v:,} µs/txn", f"{cheapest['name']}, CPU per transaction"),
        ("Costliest client", f"{dear_v:,} µs/txn", f"{dearest['name']} at 512 threads"),
    ]
    kpi_html = "\n".join(
        f'<div class="tile"><div class="tile-label">{l}</div>'
        f'<div class="tile-value">{v}</div><div class="tile-sub">{s}</div></div>'
        for l, v, s in kpis)

    def table(key, fmt):
        head = "".join(f'<th class="num">{t}</th>' for t in threads)
        body = ""
        for s in series:
            cells = "".join(
                f'<td class="num">{fmt(v) if v is not None else "–"}</td>'
                for v in s[key])
            body += f'<tr><th scope="row">{s["name"]}</th>{cells}</tr>\n'
        return (f'<table><thead><tr><th>Language</th>{head}</tr></thead>'
                f'<tbody>{body}</tbody></table>')

    html = HTML_TEMPLATE
    for k, v in {
        "__DATA__": json.dumps(data, separators=(",", ":")),
        "__KPIS__": kpi_html,
        "__TBL_TPS__": table("tps", lambda v: f"{v:,.0f}"),
        "__TBL_CLI__": table("cli", lambda v: f"{v:,}"),
        "__TBL_SRV__": table("srv", lambda v: f"{v:,}"),
    }.items():
        html = html.replace(k, v)
    OUT.write_text(html)
    print(f"written: {OUT} ({len(series)} languages, {len(threads)} thread counts)")


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
    --cat-9: #84b000; --cat-10: #0092c4; --cat-11: #8c5a2b; --cat-12: #b5174e;
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
      --cat-9: #a3c636; --cat-10: #35b5e0; --cat-11: #b07845; --cat-12: #d6447c;
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
    --cat-9: #a3c636; --cat-10: #35b5e0; --cat-11: #b07845; --cat-12: #d6447c;
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
  .notes { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 4px 20px; }
  .notes li { margin: 10px 0; color: var(--text-secondary); }
  .notes b { color: var(--text-primary); font-weight: 600; }
  footer { color: var(--muted); font-size: 12px; margin-top: 30px; max-width: 86ch; }
</style>
</head>
<body class="viz-root">
<div class="wrap">
  <header class="page">
    <div>
      <h1>MySQL client benchmark — throughput &amp; CPU cost by language</h1>
      <p class="context">sysbench <code>oltp_read_write</code>-style transactions (10 point selects,
      4 range selects, 2 updates, delete+insert per txn), 10&nbsp;M-row table, Percona Server 8.4.11,
      one language per pristine data load, client and server on the same 80-core host.
      Workers: 4&nbsp;&rarr;&nbsp;512.</p>
    </div>
    <button id="theme-toggle" type="button">Toggle dark mode</button>
  </header>

  <div class="kpis">__KPIS__</div>

  <section>
    <h2>Throughput by worker count</h2>
    <p class="sub">All languages on one chart, transactions per second against worker count.
    Each language keeps its own color and marker shape everywhere in this report;
    lines are also named at their right end. Hover for every language&rsquo;s value.</p>
    <div class="legend" id="tps-legend"></div>
    <div class="bigchart" id="tps-big"></div>
    <details class="tables"><summary>Table view — transactions per second</summary>
      <div class="tablewrap">__TBL_TPS__</div></details>
  </section>

  <section>
    <h2>CPU cost per transaction</h2>
    <p class="sub">Client CPU microseconds per committed transaction — same color and marker
    shape per language as above. The gray band is the server (mysqld) cost across all
    languages, min to max, on the same axis: near-uniform, because every client executes the
    same statements.</p>
    <div class="legend" id="cpu-legend"></div>
    <div class="bigchart" id="cpu-big"></div>
    <details class="tables"><summary>Table view — client CPU &micro;s per transaction</summary>
      <div class="tablewrap">__TBL_CLI__</div></details>
    <details class="tables"><summary>Table view — server CPU &micro;s per transaction</summary>
      <div class="tablewrap">__TBL_SRV__</div></details>
  </section>

  <section>
    <h2>Findings</h2>
    <ul class="notes">
      <li><b>Server CPU per transaction is nearly identical for every language</b>
        (~1.6&nbsp;ms at low concurrency, ~2.1&ndash;2.6&nbsp;ms under contention) — expected, since all
        clients execute the same statements, and a useful validity check for the whole comparison.</li>
      <li><b>C, C++, Swift, Rust and PHP saturate the server</b> (~29&nbsp;k tps at 128&ndash;256 workers)
        while spending only 0.3&ndash;0.9&nbsp;ms of client CPU per transaction — the server is always
        their bottleneck.</li>
      <li><b>Go&rsquo;s collapse at 32&ndash;128 workers is client-side runtime burn</b>: its client CPU jumps
        from ~0.6&nbsp;ms to ~4&nbsp;ms per transaction (GC/scheduler defaults on an 80-core client;
        <code>GOMAXPROCS=16 GOGC=800</code> recovers ~3&times;). At 512 workers the Go client consumes more
        total CPU than mysqld.</li>
      <li><b>JavaScript and TypeScript are the costliest clients</b> (~2.8&nbsp;ms/txn at 4 workers,
        ~12.4&nbsp;ms at 512) — mysql2&rsquo;s pure-JS protocol parsing. TypeScript tracks JavaScript
        within a few percent everywhere: compilation adds no runtime cost.</li>
      <li><b>C# scales to 64 workers then degrades</b> under the default workstation GC
        (<code>DOTNET_gcServer=1</code> measured 2.4&times; faster at 256 workers).</li>
      <li><b>Python stays at ~1&nbsp;k tps regardless of workers</b> (GIL), and its text protocol
        (no server-side prepares) costs the <em>server</em> ~18% extra CPU per transaction.</li>
    </ul>
  </section>

  <footer>
    Methodology: per-worker PRNG with identical streams across languages; warmup excluded from the
    timed window via a start barrier; retries on deadlock/lock-wait/duplicate-key with full redraw.
    CPU window covers the full run (connect + warmup + timed + teardown), slightly wider than the
    throughput window but identical in shape for every language. Client CPU = user+sys of the whole
    benchmark process tree; server CPU = mysqld utime+stime delta. Sources:
    <code>results/&lt;host&gt;/*.json</code>, <code>results/summary.csv</code>. Regenerate with
    <code>python3 report/generate_html_report.py</code>.
  </footer>
</div>

<div id="tooltip" role="status"></div>

<script>
const DATA = __DATA__;
(function () {
  "use strict";
  const T = DATA.threads, N = T.length;
  const tooltip = document.getElementById("tooltip");
  const fmt = v => v >= 10000 ? Math.round(v / 1000) + "K" : Math.round(v).toLocaleString("en-US");
  const fmtFull = v => Math.round(v).toLocaleString("en-US");
  const ns = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => {
    const e = document.createElementNS(ns, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  // Identity encoding, fixed per LANGUAGE (color follows the entity, never
  // its rank): every language has a unique solid hue; marker shape is the
  // secondary identity channel.
  // Circle and square are used once each; the remaining shapes repeat at most
  // twice, always pairing strongly contrasting hues.
  const STYLE = {
    c:          { slot: 1,  shape: "circle"   }, // blue
    cpp:        { slot: 2,  shape: "square"   }, // orange
    swift:      { slot: 3,  shape: "triangle" }, // aqua
    rust:       { slot: 8,  shape: "diamond"  }, // red
    php:        { slot: 5,  shape: "tridown"  }, // magenta
    java:       { slot: 6,  shape: "plus"     }, // green
    csharp:     { slot: 11, shape: "star"     }, // brown
    go:         { slot: 10, shape: "diamond"  }, // cyan   (vs rust red)
    javascript: { slot: 4,  shape: "tridown"  }, // yellow (vs php magenta)
    typescript: { slot: 9,  shape: "star"     }, // lime   (vs csharp brown)
    python:     { slot: 7,  shape: "plus"     }, // purple (vs java green)
    ruby:       { slot: 12, shape: "triangle" }, // crimson (vs swift aqua)
  };
  const styleFor = lang => {
    const s = STYLE[lang] || { slot: 1, shape: "circle" };
    return { color: `var(--cat-${s.slot})`, dash: null, shape: s.shape };
  };

  // Marker: >= 8px across, 2px surface ring so it reads over lines.
  function shapeNode(shape, x, y, color, surface) {
    const common = { fill: color, stroke: surface, "stroke-width": 2, "stroke-linejoin": "round" };
    const s = 4.6; // half-extent
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
    return el("path", { d: `M ${x} ${y - s - 1} L ${x + s + 1} ${y} L ${x} ${y + s + 1} L ${x - s - 1} ${y} Z`, ...common }); // diamond
  }

  // Legend / tooltip key: a short stroke (dash rendered exactly as in the
  // chart) with the series marker on it — CSS dashed borders render nearly
  // solid at this size, so keys are real SVG.
  function keySVG(st, w, h) {
    const svg = el("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    const line = el("line", { x1: 1, x2: w - 1, y1: h / 2, y2: h / 2, stroke: st.color, "stroke-width": 2, "stroke-linecap": "round" });
    if (st.dash) line.setAttribute("stroke-dasharray", "5 4");
    svg.appendChild(line);
    svg.appendChild(shapeNode(st.shape, w / 2, h / 2, st.color, "var(--surface-1)"));
    return svg;
  }

  function legendInto(hostId, entries) {
    const leg = document.getElementById(hostId);
    for (const e of entries) {
      const item = document.createElement("span");
      item.className = "item";
      item.appendChild(e.key);
      item.appendChild(document.createTextNode(e.name));
      leg.appendChild(item);
    }
  }

  // One large multi-series line chart with optional context band.
  // cfg: {hostId, seriesDefs:[{name, vals, st}], yMax, yStep, unit,
  //       band: {lo, hi, name} | null, ariaLabel}
  function bigChart(cfg) {
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
      const down = cfg.band.lo.map((v, i) => "L" + X(N - 1 - i).toFixed(1) + " " + Y(cfg.band.lo[N - 1 - i]).toFixed(1)).join(" ");
      svg.appendChild(el("path", { d: up + " " + down + " Z", fill: "var(--band)", opacity: 0.16, stroke: "none" }));
    }
    const path = vals => vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
    for (let si = cfg.seriesDefs.length - 1; si >= 0; si--) {
      const d = cfg.seriesDefs[si];
      const attrs = { d: path(d.vals), fill: "none", stroke: d.st.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" };
      if (d.st.dash) attrs["stroke-dasharray"] = d.st.dash;
      svg.appendChild(el("path", attrs));
      for (let i = 0; i < N; i++) {
        svg.appendChild(shapeNode(d.st.shape, X(i), Y(d.vals[i]), d.st.color, "var(--surface-1)"));
      }
    }
    // collision-resolved end labels with leader lines
    const labels = cfg.seriesDefs.map(d => ({ d, endY: Y(d.vals[N - 1]), y: Y(d.vals[N - 1]) }))
      .sort((a, b) => a.y - b.y);
    if (cfg.band) labels.push({ band: true, endY: Y((cfg.band.lo[N - 1] + cfg.band.hi[N - 1]) / 2), y: Y((cfg.band.lo[N - 1] + cfg.band.hi[N - 1]) / 2) });
    labels.sort((a, b) => a.y - b.y);
    const GAPL = 16;
    for (let k = 1; k < labels.length; k++) {
      if (labels[k].y - labels[k - 1].y < GAPL) labels[k].y = labels[k - 1].y + GAPL;
    }
    const over = labels[labels.length - 1].y - (BM.t + BIH + 6);
    if (over > 0) for (const L of labels) L.y -= over * (L.y - BM.t) / BIH;
    for (const L of labels) {
      if (Math.abs(L.y - L.endY) > 3) {
        svg.appendChild(el("line", { x1: X(N - 1) + 7, y1: L.endY, x2: X(N - 1) + 22, y2: L.y, stroke: "var(--baseline)", "stroke-width": 1 }));
      }
      const t = el("text", { x: X(N - 1) + 26, y: L.y + 3.5, class: "seriesname" });
      t.textContent = L.band ? cfg.band.name : L.d.name + "  " + fmt(L.d.vals[N - 1]);
      if (L.band) t.setAttribute("fill", "var(--muted)");
      svg.appendChild(t);
    }
    // hover: crosshair + ranked all-series tooltip; keyboard navigable
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
        val.textContent = fmtFull(d.vals[i]) + (cfg.unit ? " " + cfg.unit : "");
        const name = document.createElement("span"); name.className = "tt-name"; name.textContent = d.name;
        row.append(val, name); tooltip.appendChild(row);
      }
      if (cfg.band) {
        const row = document.createElement("div"); row.className = "tt-row";
        const sw = document.createElement("span");
        sw.style.cssText = "width:20px;height:9px;background:var(--band);opacity:.25;border-radius:2px;flex:none;";
        const val = document.createElement("span"); val.className = "tt-val";
        val.textContent = fmtFull(DATA.srvLo[i]) + "–" + fmtFull(DATA.srvHi[i]) + (cfg.unit ? " " + cfg.unit : "");
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
    document.getElementById(cfg.hostId).appendChild(svg);
  }

  const defsFor = key => DATA.series.map(s => ({ name: s.name, vals: s[key], st: styleFor(s.lang) }));
  const langLegend = DATA.series.map(s => ({ name: s.name, key: keySVG(styleFor(s.lang), 26, 12) }));

  // --- TPS: one large chart -------------------------------------------------
  const tpsMax = Math.ceil(Math.max(...DATA.series.map(s => Math.max(...s.tps))) / 10000) * 10000;
  legendInto("tps-legend", langLegend);
  bigChart({
    hostId: "tps-big", seriesDefs: defsFor("tps"), yMax: tpsMax, yStep: 5000,
    unit: "tps", band: null,
    ariaLabel: "Transactions per second by worker count, all languages",
  });

  // --- CPU: one large chart (client lines + server band) -------------------
  const cpuMax = Math.ceil(Math.max(...DATA.series.map(s => Math.max(...s.cli)), ...DATA.srvHi) / 2000) * 2000;
  const bandKey = document.createElement("span");
  bandKey.style.cssText = "width:26px;height:10px;background:var(--band);opacity:.25;border-radius:2px;display:inline-block;";
  legendInto("cpu-legend", DATA.series.map(s => ({ name: s.name, key: keySVG(styleFor(s.lang), 26, 12) }))
    .concat([{ name: "Server (mysqld), min–max", key: bandKey }]));
  bigChart({
    hostId: "cpu-big", seriesDefs: defsFor("cli"), yMax: cpuMax, yStep: 2000,
    unit: "µs", band: { lo: DATA.srvLo, hi: DATA.srvHi, name: "Server (mysqld)" },
    ariaLabel: "Client CPU microseconds per transaction by worker count, all languages, with server CPU range band",
  });

  // --- theme toggle ---
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
