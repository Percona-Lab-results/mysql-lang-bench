#!/usr/bin/env python3
"""Generate report/report.html — self-contained HTML benchmark report.

Reads results/<host>/*.json (latest run per (lang, threads), v2 rows with CPU
fields), embeds the data as JSON, and renders:
  - KPI stat tiles
  - TPS vs threads: emphasis small multiples (one facet per language, all
    other languages as gray ghost lines)
  - CPU cost per transaction: small multiples with two series (client, server)
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
    "typescript": "TypeScript", "python": "Python",
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
            if len(drv) > 34:
                drv = drv[:33] + "…"
        series.append({
            "lang": lang, "name": DISPLAY.get(lang, lang), "driver": drv,
            "tps": tps, "cli": cli, "srv": srv,
            "peak": max(v for v in tps if v is not None),
        })
    series.sort(key=lambda s: -s["peak"])

    best = series[0]
    peak_t = threads[best["tps"].index(best["peak"])]
    cheapest = min(series, key=lambda s: min(v for v in s["cli"] if v is not None))
    cheap_v = min(v for v in cheapest["cli"] if v is not None)
    dearest = max(series, key=lambda s: max(v for v in s["cli"] if v is not None))
    dear_v = max(v for v in dearest["cli"] if v is not None)
    srv_low = round(sum(s["srv"][0] for s in series) / len(series))

    data = {"threads": threads, "series": series}
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

    tbl_tps = table("tps", lambda v: f"{v:,.0f}")
    tbl_cli = table("cli", lambda v: f"{v:,}")
    tbl_srv = table("srv", lambda v: f"{v:,}")

    html = HTML_TEMPLATE
    for k, v in {
        "__DATA__": json.dumps(data, separators=(",", ":")),
        "__KPIS__": kpi_html,
        "__TBL_TPS__": tbl_tps,
        "__TBL_CLI__": tbl_cli,
        "__TBL_SRV__": tbl_srv,
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
    --grid: #e1e0d9; --baseline: #c3c2b7; --ghost: #c9c8c0;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #eb6834;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --ghost: #3d3d39;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --series-2: #d95926;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --ghost: #3d3d39;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926;
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
  .legend { display: flex; gap: 18px; align-items: center; margin: 0 0 10px; color: var(--text-secondary); font-size: 13px; }
  .legend .key { display: inline-block; width: 18px; height: 0; border-top: 2.5px solid; border-radius: 2px; margin-right: 6px; vertical-align: middle; }
  .facets { display: grid; grid-template-columns: repeat(auto-fill, minmax(255px, 1fr)); gap: 14px; }
  .facet { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px 6px; }
  .facet h3 { font-size: 13px; font-weight: 600; margin: 0; }
  .facet .drv { color: var(--muted); font-size: 11px; margin: 0 0 4px; }
  .facet svg { display: block; width: 100%; height: auto; }
  .facet svg:focus { outline: 2px solid var(--series-1); outline-offset: 2px; border-radius: 4px; }
  svg text { font-family: inherit; font-size: 10.5px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  svg .endlab { fill: var(--text-secondary); font-weight: 600; font-size: 11px; }
  #tooltip {
    position: fixed; pointer-events: none; z-index: 10; display: none;
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.18); padding: 8px 11px; font-size: 12.5px; min-width: 130px;
  }
  #tooltip .tt-title { color: var(--muted); margin-bottom: 4px; }
  #tooltip .tt-row { display: flex; align-items: center; gap: 7px; margin-top: 2px; }
  #tooltip .tt-key { width: 14px; height: 0; border-top: 2.5px solid; border-radius: 2px; flex: none; }
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
    <p class="sub">Each panel highlights one language; the gray lines are all other
    languages for context. Panels are ordered by peak throughput and share the same scale.</p>
    <div class="facets" id="tps-facets"></div>
    <details class="tables"><summary>Table view — transactions per second</summary>
      <div class="tablewrap">__TBL_TPS__</div></details>
  </section>

  <section>
    <h2>CPU cost per transaction</h2>
    <p class="sub">CPU seconds consumed per committed transaction, split between the
    benchmark client and mysqld, measured over the same window on the same host.
    Server cost is near-uniform across languages — the client lines are the language story.</p>
    <div class="legend">
      <span><span class="key" style="border-color: var(--series-1)"></span>Client (benchmark process)</span>
      <span><span class="key" style="border-color: var(--series-2)"></span>Server (mysqld)</span>
    </div>
    <div class="facets" id="cpu-facets"></div>
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
  const W = 290, H = 170, M = { l: 40, r: 56, t: 10, b: 24 };
  const IW = W - M.l - M.r, IH = H - M.t - M.b;
  const X = i => M.l + (N === 1 ? IW / 2 : i * IW / (N - 1));
  const tooltip = document.getElementById("tooltip");
  // One tick style per axis: compact every tick when the axis reaches 10K.
  const fmtAxis = (v, axisMax) => v === 0 ? "0" : axisMax >= 10000 ? (v / 1000) + "K" : v.toLocaleString("en-US");
  const fmt = v => v >= 10000 ? Math.round(v / 1000) + "K" : Math.round(v).toLocaleString("en-US");
  const fmtFull = v => Math.round(v).toLocaleString("en-US");
  const ns = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => {
    const e = document.createElementNS(ns, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  function niceTicks(max) {
    const step = max > 20000 ? 10000 : max > 8000 ? 4000 : max > 2000 ? 1000 : 500;
    const out = [];
    for (let v = 0; v <= max; v += step) out.push(v);
    return out;
  }

  // svgFor: builds one facet svg with grid, axes, series lines, crosshair + hover.
  // seriesDefs: [{vals, color(css var), ghost:bool, marker:bool, endLabel:bool}]
  // ttRows(i): [{color, value, name}]
  function facetSVG(seriesDefs, yMax, ttRows, ariaLabel) {
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, tabindex: "0", role: "img", "aria-label": ariaLabel });
    const Y = v => M.t + IH - (v / yMax) * IH;
    for (const tv of niceTicks(yMax)) {
      svg.appendChild(el("line", { x1: M.l, x2: W - M.r, y1: Y(tv), y2: Y(tv), stroke: "var(--grid)", "stroke-width": 1 }));
      const t = el("text", { x: M.l - 5, y: Y(tv) + 3.5, "text-anchor": "end" });
      t.textContent = fmtAxis(tv, yMax);
      svg.appendChild(t);
    }
    svg.appendChild(el("line", { x1: M.l, x2: W - M.r, y1: M.t + IH, y2: M.t + IH, stroke: "var(--baseline)", "stroke-width": 1 }));
    for (const ti of [0, 3, 5, 7]) {
      if (ti >= N) continue;
      const t = el("text", { x: X(ti), y: H - 8, "text-anchor": "middle" });
      t.textContent = T[ti];
      svg.appendChild(t);
    }
    const path = vals => vals.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
    for (const s of seriesDefs.filter(s => s.ghost)) {
      svg.appendChild(el("path", { d: path(s.vals), fill: "none", stroke: "var(--ghost)", "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    for (const s of seriesDefs.filter(s => !s.ghost)) {
      svg.appendChild(el("path", { d: path(s.vals), fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      if (s.marker) {
        svg.appendChild(el("circle", { cx: X(N - 1), cy: Y(s.vals[N - 1]), r: 4, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2 }));
      }
      if (s.endLabel) {
        const t = el("text", { x: X(N - 1) + 8, y: Y(s.vals[N - 1]) + 3.5, class: "endlab" });
        t.textContent = fmt(s.vals[N - 1]);
        svg.appendChild(t);
      }
    }
    // hover layer: crosshair snapping to nearest thread index
    const cross = el("line", { y1: M.t, y2: M.t + IH, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
    svg.appendChild(cross);
    let idx = N - 1;
    function show(i, cx, cy) {
      idx = i;
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i));
      cross.setAttribute("visibility", "visible");
      tooltip.replaceChildren();
      const title = document.createElement("div");
      title.className = "tt-title";
      title.textContent = T[i] + " threads";
      tooltip.appendChild(title);
      for (const r of ttRows(i)) {
        const row = document.createElement("div"); row.className = "tt-row";
        const key = document.createElement("span"); key.className = "tt-key"; key.style.borderColor = r.color;
        const val = document.createElement("span"); val.className = "tt-val"; val.textContent = r.value;
        const name = document.createElement("span"); name.className = "tt-name"; name.textContent = r.name;
        row.append(key, val, name); tooltip.appendChild(row);
      }
      tooltip.style.display = "block";
      const tw = tooltip.offsetWidth, pad = 14;
      let left = cx + pad;
      if (left + tw > window.innerWidth - 8) left = cx - tw - pad;
      tooltip.style.left = left + "px";
      tooltip.style.top = Math.max(8, cy - 10) + "px";
    }
    function hide() { cross.setAttribute("visibility", "hidden"); tooltip.style.display = "none"; }
    svg.addEventListener("pointermove", ev => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * (W / rect.width);
      let best = 0, bd = Infinity;
      for (let i = 0; i < N; i++) { const d = Math.abs(px - X(i)); if (d < bd) { bd = d; best = i; } }
      show(best, ev.clientX, ev.clientY);
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", () => {
      const r = svg.getBoundingClientRect();
      show(idx, r.left + (X(idx) * r.width / W), r.top + r.height / 2);
    });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", ev => {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        const i = Math.min(N - 1, Math.max(0, idx + (ev.key === "ArrowRight" ? 1 : -1)));
        const r = svg.getBoundingClientRect();
        show(i, r.left + (X(i) * r.width / W), r.top + r.height / 2);
      } else if (ev.key === "Escape") hide();
    });
    return svg;
  }

  function facetCard(s, svg) {
    const card = document.createElement("div");
    card.className = "facet";
    const h = document.createElement("h3"); h.textContent = s.name;
    const d = document.createElement("p"); d.className = "drv"; d.textContent = s.driver;
    card.append(h, d, svg);
    return card;
  }

  // --- TPS small multiples (emphasis) ---
  const tpsMax = Math.max(...DATA.series.map(s => Math.max(...s.tps)));
  const tpsHost = document.getElementById("tps-facets");
  for (const s of DATA.series) {
    const defs = DATA.series.filter(o => o !== s).map(o => ({ vals: o.tps, ghost: true }));
    defs.push({ vals: s.tps, color: "var(--series-1)", marker: true, endLabel: true });
    const svg = facetSVG(defs, Math.ceil(tpsMax / 10000) * 10000,
      i => [{ color: "var(--series-1)", value: fmtFull(s.tps[i]) + " tps", name: s.name }],
      s.name + ": transactions per second by worker count");
    tpsHost.appendChild(facetCard(s, svg));
  }

  // --- CPU small multiples (client + server) ---
  const cpuMax = Math.max(...DATA.series.map(s => Math.max(...s.cli, ...s.srv)));
  const cpuHost = document.getElementById("cpu-facets");
  for (const s of DATA.series) {
    const defs = [
      { vals: s.srv, color: "var(--series-2)", marker: true },
      { vals: s.cli, color: "var(--series-1)", marker: true, endLabel: true },
    ];
    const svg = facetSVG(defs, Math.ceil(cpuMax / 4000) * 4000,
      i => [
        { color: "var(--series-1)", value: fmtFull(s.cli[i]) + " µs", name: "client" },
        { color: "var(--series-2)", value: fmtFull(s.srv[i]) + " µs", name: "server" },
      ],
      s.name + ": client and server CPU microseconds per transaction");
    cpuHost.appendChild(facetCard(s, svg));
  }

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
