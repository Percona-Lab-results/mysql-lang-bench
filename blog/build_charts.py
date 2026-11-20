#!/usr/bin/env python3
"""Fill the chart blocks in the blog HTML from runs/avg.json.

Replaces everything between <!-- chart:start --> and <!-- chart:end --> in
post-1-scripting-languages.html with a static SVG line chart (readable without
JavaScript), a small progressive-enhancement hover script, and a data table.
Run from anywhere:  python3 blog/build_charts.py
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLOG = ROOT / "blog"
DATA = json.loads((ROOT / "runs" / "avg.json").read_text())

THREADS = [8, 32, 128, 512]
# (lang key, label, CSS colour var). C is a neutral reference line.
SERIES = [
    ("c", "C (native reference)", "--ref"),
    ("lua", "Lua", "--s2"),
    ("php", "PHP", "--s1"),
    ("javascript", "JavaScript", "--s3"),
    ("python", "Python", "--s4"),
    ("ruby", "Ruby", "--s5"),
]


def values(workload, lang):
    by_t = {v["threads"]: v["throughput"] for v in DATA.values()
            if v["workload"] == workload and v["lang"] == lang}
    return [by_t[t] for t in THREADS]


def fmt_m(x):
    return f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.0f}K"


def line_chart(workload, chart_id):
    W, H = 720, 400
    L, R, T, B = 52, 148, 16, 44
    pw, ph = W - L - R, H - T - B
    ymax = 30e6
    xs = [L + i * pw / (len(THREADS) - 1) for i in range(len(THREADS))]
    y = lambda v: T + ph * (1 - v / ymax)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{chart_id}-t {chart_id}-d" '
           f'xmlns="http://www.w3.org/2000/svg">']
    # Gridlines and y ticks.
    for tick in range(0, 31, 5):
        ty = y(tick * 1e6)
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{ty:.1f}" y2="{ty:.1f}" '
                   f'style="stroke:var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{L - 8}" y="{ty + 4:.1f}" text-anchor="end" font-size="12" '
                   f'style="fill:var(--muted)">{tick}M</text>')
    # X ticks.
    for t, x in zip(THREADS, xs):
        out.append(f'<text x="{x:.1f}" y="{T + ph + 20}" text-anchor="middle" font-size="12" '
                   f'style="fill:var(--muted)">{t}</text>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{H - 4}" text-anchor="middle" font-size="12" '
               f'style="fill:var(--ink-2)">Concurrent workers</text>')

    # Lines and end markers.
    ends = []
    for lang, label, var in SERIES:
        vs = values(workload, lang)
        pts = " ".join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, vs))
        out.append(f'<polyline points="{pts}" fill="none" style="stroke:var({var})" '
                   f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        out.append(f'<circle cx="{xs[-1]:.1f}" cy="{y(vs[-1]):.1f}" r="4" style="fill:var({var});'
                   f'stroke:var(--bg)" stroke-width="2"/>')
        ends.append([y(vs[-1]), label.split(" (")[0], vs[-1]])

    # Direct end labels, nudged apart so they never overlap.
    ends.sort()
    gap = 15
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + gap)
    overflow = ends[-1][0] - (T + ph + 4)
    if overflow > 0:
        for e in ends:
            e[0] -= overflow
        for i in range(len(ends) - 2, -1, -1):
            ends[i][0] = min(ends[i][0], ends[i + 1][0] - gap)
    for ly, name, v in ends:
        out.append(f'<text x="{xs[-1] + 10:.1f}" y="{ly + 4:.1f}" font-size="12" '
                   f'style="fill:var(--ink-2)">{name} <tspan style="fill:var(--muted)">{fmt_m(v)}</tspan></text>')

    # Hover layer (used only if the script runs).
    out.append(f'<line class="xhair" x1="0" x2="0" y1="{T}" y2="{T + ph}" '
               f'style="stroke:var(--muted);display:none" stroke-width="1"/>')
    out.append(f'<rect class="hit" x="{L - 20}" y="{T}" width="{pw + 40}" height="{ph}" fill="transparent"/>')
    out.append("</svg>")
    return "\n".join(out), xs, W


def table(workload, caption):
    head = "".join(f"<th>{t}</th>" for t in THREADS)
    rows = []
    for lang, label, _ in SERIES:
        cells = "".join(f"<td>{v / 1e6:.2f}</td>" for v in values(workload, lang))
        rows.append(f"<tr><td>{label.split(' (')[0]}</td>{cells}</tr>")
    return (f'<details><summary>{caption}</summary><div class="table-wrap"><table>'
            f'<thead><tr><th>Workers</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody>'
            f'</table></div></details>')


def hover_script(chart_id, xs, width, workload):
    data = {label.split(" (")[0]: {"var": var, "v": values(workload, lang)}
            for lang, label, var in SERIES}
    return f"""<script>
(function () {{
  var root = document.getElementById("{chart_id}");
  if (!root) return;
  var svg = root.querySelector("svg"), hit = svg.querySelector(".hit"),
      xh = svg.querySelector(".xhair"), tip = root.querySelector(".tip");
  var xs = {json.dumps([round(x, 1) for x in xs])}, threads = {json.dumps(THREADS)},
      W = {width}, data = {json.dumps(data)};
  function fmt(v) {{ return v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : Math.round(v / 1e3) + "K"; }}
  function show(evt) {{
    var box = svg.getBoundingClientRect(), sx = (evt.clientX - box.left) * W / box.width, i = 0;
    for (var k = 1; k < xs.length; k++) if (Math.abs(xs[k] - sx) < Math.abs(xs[i] - sx)) i = k;
    xh.setAttribute("x1", xs[i]); xh.setAttribute("x2", xs[i]); xh.style.display = "";
    var names = Object.keys(data).sort(function (a, b) {{ return data[b].v[i] - data[a].v[i]; }});
    var html = "<div><b>" + threads[i] + " workers</b> · rows/s</div>";
    names.forEach(function (n) {{
      html += '<div class="row"><span><i class="sw" style="background:var(' + data[n].var + ')"></i>' +
              n + "</span><span>" + fmt(data[n].v[i]) + "</span></div>";
    }});
    tip.innerHTML = html; tip.style.display = "block";
    var px = xs[i] * box.width / W, left = px + 14;
    if (left + tip.offsetWidth > root.clientWidth) left = px - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + "px"; tip.style.top = "40px";
  }}
  function hide() {{ xh.style.display = "none"; tip.style.display = "none"; }}
  hit.addEventListener("mousemove", show); hit.addEventListener("mouseleave", hide);
  hit.addEventListener("touchstart", function (e) {{ show(e.touches[0]); }}, {{ passive: true }});
}})();
</script>"""


def build_post1():
    cid = "w2-chart"
    svg, xs, width = line_chart(2, cid)
    legend = "".join(f'<li><i class="sw" style="background:var({var})"></i>{label}</li>'
                     for _, label, var in SERIES)
    block = f"""<!-- chart:start -->
<figure id="{cid}">
  <p class="chart-title" id="{cid}-t">Rows read and summed per second, by number of workers</p>
  <p class="chart-sub" id="{cid}-d">PHP and Lua scale almost like C. Python stays near 2 million rows/s and Ruby near 150,000, however many workers run.</p>
  <ul class="legend">{legend}</ul>
  <div class="chart">
{svg}
    <div class="tip" role="status"></div>
  </div>
  <figcaption>Workload 2: each worker fetches 100,000 integers with one prepared SELECT and sums them. Average of three runs. Hover or tap the chart for exact values.</figcaption>
  {table(2, "Show the numbers (million rows per second)")}
</figure>
{hover_script(cid, xs, width, 2)}
<!-- chart:end -->"""
    path = BLOG / "post-1-scripting-languages.html"
    html = path.read_text()
    html, n = re.subn(r"<!-- chart:start -->.*?<!-- chart:end -->", lambda m: block, html, flags=re.S)
    assert n == 1, "chart markers not found"
    path.write_text(html)
    print("updated", path)


if __name__ == "__main__":
    build_post1()
