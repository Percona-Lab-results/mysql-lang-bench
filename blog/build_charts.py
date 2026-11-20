#!/usr/bin/env python3
"""Render the blog chart images from runs/avg.json.

Builds static SVG charts for each post and renders them to 2x PNGs in
blog/png/ with headless Chromium. The intermediate HTML goes to blog/build/,
which git ignores.

Run from anywhere:  python3 blog/build_charts.py
"""
import json
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLOG = ROOT / "blog"
BUILD = BLOG / "build"
PNG = BLOG / "png"
DATA = json.loads((ROOT / "runs" / "avg.json").read_text())

THREADS = [4, 8, 16, 32, 64, 128, 256, 512]

# Light-mode palette (validated reference categorical slots 1-5, gray for C).
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#75746f"
GRID, AXIS, BG = "#e8e7e3", "#52514e", "#ffffff"
REF, S1, S2, S3, S4, S5 = "#8a8984", "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"
FONT = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"

NAMES = {"c": "C", "cpp": "C++", "csharp": "C#", "go": "Go", "java": "Java", "javascript": "JavaScript",
         "lua": "Lua", "php": "PHP", "python": "Python", "ruby": "Ruby", "rust": "Rust",
         "swift": "Swift", "typescript": "TypeScript"}

# Post 1: integer read. (lang key, legend label, colour).
POST1_SERIES = [
    ("c", "C (native reference)", REF),
    ("lua", "Lua", S2),
    ("php", "PHP", S1),
    ("javascript", "JavaScript", S3),
    ("python", "Python", S4),
    ("ruby", "Ruby", S5),
]
# Post 2: integer insert. One language per group, C as the gray reference.
POST2_SERIES = [
    ("c", "C (native reference)", REF),
    ("php", "PHP", S1),
    ("java", "Java", S2),
    ("javascript", "JavaScript", S3),
    ("python", "Python", S4),
    ("ruby", "Ruby", S5),
]


def run(workload, lang, threads):
    for v in DATA.values():
        if v["workload"] == workload and v["lang"] == lang and v["threads"] == threads:
            return v
    raise KeyError((workload, lang, threads))


def values(workload, lang):
    return [run(workload, lang, t)["throughput"] for t in THREADS]


def fmt_m(x):
    return f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.0f}K"


def fmt_tick_m(t):
    return f"{t / 1e6:.0f}M"


def fmt_tick_k(t):
    return f"{t / 1e3:.0f}K"


def svg_open(W, H):
    return f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="{FONT}">'


def line_chart_svg(workload, series, ymax, ytick, tick_fmt, end_fmt, ylabel, left=56):
    W, H = 720, 400
    L, R, T, B = left, 148, 16, 48
    pw, ph = W - L - R, H - T - B
    xs = [L + i * pw / (len(THREADS) - 1) for i in range(len(THREADS))]
    y = lambda v: T + ph * (1 - v / ymax)

    out = [svg_open(W, H)]
    # Horizontal gridlines and y tick labels.
    t = 0
    while t <= ymax + 1:
        ty = y(t)
        if t > 0:
            out.append(f'<line x1="{L}" x2="{L + pw}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<line x1="{L - 5}" x2="{L}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{L - 9}" y="{ty + 4:.1f}" text-anchor="end" font-size="12" fill="{MUTED}">{tick_fmt(t)}</text>')
        t += ytick
    # Vertical gridlines at every worker count, with tick marks and labels.
    for n, x in zip(THREADS, xs):
        if x > L:
            out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{T + ph}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T + ph}" y2="{T + ph + 5}" stroke="{AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{x:.1f}" y="{T + ph + 20}" text-anchor="middle" font-size="12" fill="{MUTED}">{n}</text>')
    # Axis lines, drawn over the grid.
    out.append(f'<line x1="{L}" x2="{L}" y1="{T}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{H - 6}" text-anchor="middle" font-size="12" fill="{INK2}">Concurrent workers</text>')
    out.append(f'<text transform="translate(14 {T + ph / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{INK2}">{ylabel}</text>')

    # Lines, an end marker and end labels.
    ends = []
    for lang, label, colour in series:
        vs = values(workload, lang)
        pts = " ".join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, vs))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        out.append(f'<circle cx="{xs[-1]:.1f}" cy="{y(vs[-1]):.1f}" r="4" fill="{colour}" stroke="{BG}" stroke-width="2"/>')
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
        out.append(f'<text x="{xs[-1] + 10:.1f}" y="{ly + 4:.1f}" font-size="12" fill="{INK2}">'
                   f'{name} <tspan fill="{MUTED}">{end_fmt(v)}</tspan></text>')
    out.append("</svg>")
    return "\n".join(out)


def bar_chart_svg(rows, xmax, xtick, xlabel, value_fmt, colour=S1):
    """Horizontal bars, one per (label, value) row, in the order given."""
    bar_h, gap = 18, 8
    L, R, T, B = 96, 64, 8, 44
    W = 720
    ph = len(rows) * (bar_h + gap) - gap
    H = T + ph + B
    pw = W - L - R
    x = lambda v: L + pw * v / xmax

    out = [svg_open(W, H)]
    t = 0
    while t <= xmax + 1e-9:
        tx = x(t)
        if t > 0:
            out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{T}" y2="{T + ph}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{T + ph}" y2="{T + ph + 5}" stroke="{AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{tx:.1f}" y="{T + ph + 20}" text-anchor="middle" font-size="12" fill="{MUTED}">{t:g}</text>')
        t += xtick
    for i, (label, v) in enumerate(rows):
        by = T + i * (bar_h + gap)
        w = max(x(v) - L, 1)
        r = min(4, w / 2)
        # Bar with a rounded data end, square at the baseline.
        out.append(f'<path d="M{L},{by} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{bar_h - 2 * r} '
                   f'a{r},{r} 0 0 1 {-r},{r} h{-(w - r):.1f} z" fill="{colour}"/>')
        out.append(f'<text x="{L - 10}" y="{by + bar_h / 2 + 4:.1f}" text-anchor="end" font-size="12" fill="{INK2}">{label}</text>')
        out.append(f'<text x="{L + w + 6:.1f}" y="{by + bar_h / 2 + 4:.1f}" font-size="12" fill="{INK2}">{value_fmt(v)}</text>')
    out.append(f'<line x1="{L}" x2="{L}" y1="{T}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{H - 6}" text-anchor="middle" font-size="12" fill="{INK2}">{xlabel}</text>')
    out.append("</svg>")
    return "\n".join(out), H


def page(title, svg, series=None):
    legend = ""
    if series:
        legend = '<div class="legend">' + "".join(
            f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
            f'<i style="display:inline-block;width:14px;height:3px;border-radius:2px;background:{c}"></i>{lbl}</span>'
            for _, lbl, c in series) + "</div>"
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>'
            f'body{{margin:0;background:{BG};font-family:{FONT}}}'
            f'.wrap{{width:760px;padding:16px 20px}} h1{{font-size:17px;margin:0 0 6px;color:{INK}}}'
            f'.legend{{font-size:13px;color:{INK2};margin-bottom:6px}} svg{{width:100%;height:auto;display:block}}'
            f'</style></head><body><div class="wrap"><h1>{title}</h1>{legend}{svg}</div></body></html>')


def render(name, html, height=500):
    BUILD.mkdir(exist_ok=True)
    PNG.mkdir(exist_ok=True)
    src = BUILD / f"{name}.html"
    out = PNG / f"{name}.png"
    src.write_text(html)
    browser = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not browser:
        raise SystemExit("needs chromium (or google-chrome) on PATH to render PNGs")
    subprocess.run([browser, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                    "--force-device-scale-factor=2", f"--window-size=800,{height}",
                    f"--screenshot={out}", src.as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("wrote", out)


def build_post1():
    svg = line_chart_svg(2, POST1_SERIES, ymax=30e6, ytick=5e6, tick_fmt=fmt_tick_m,
                         end_fmt=fmt_m, ylabel="Rows per second")
    render("post-1-w2", page("Rows read and summed per second, by number of workers", svg, POST1_SERIES))


def build_post2():
    svg = line_chart_svg(1, POST2_SERIES, ymax=225e3, ytick=25e3, tick_fmt=fmt_tick_k,
                         end_fmt=fmt_m, ylabel="Rows inserted per second", left=70)
    render("post-2-w1", page("Rows inserted per second, by number of workers", svg, POST2_SERIES))

    # Client CPU seconds per million rows inserted, 512 workers, cheapest first.
    rows = []
    for lang, name in NAMES.items():
        r = run(1, lang, 512)
        rows.append((name, r["client_cpu_s"] / r["ops"] * 1e6))
    rows.sort(key=lambda kv: kv[1])
    svg, h = bar_chart_svg(rows, xmax=60, xtick=10, xlabel="Client CPU seconds per million rows inserted",
                           value_fmt=lambda v: f"{v:.1f} s")
    render("post-2-w1-cpu", page("Client CPU cost of inserting rows, 512 workers", svg), height=int(h * 760 / 720) + 70)


def build_post_managed():
    """Java and C# against C: integer reads and blob reads."""
    series = [
        ("c", "C (native reference)", REF),
        ("java", "Java", S2),
        ("csharp", "C#", "#6250d6"),  # reference palette slot 7 (violet)
    ]
    svg = line_chart_svg(2, series, ymax=30e6, ytick=5e6, tick_fmt=fmt_tick_m,
                         end_fmt=fmt_m, ylabel="Rows per second")
    render("post-managed-w2", page("Integer reads: rows read and summed per second", svg, series))
    svg = line_chart_svg(4, series, ymax=9e3, ytick=1e3, tick_fmt=lambda t: f"{t:,.0f}",
                         end_fmt=lambda v: f"{v:,.0f}", ylabel="Blobs per second", left=62)
    render("post-managed-w4", page("Blob reads: 1 MiB blobs read and hashed per second", svg, series))


def stacked_cores_svg(rows, unit, xmax=80):
    """rows: (label, client_cores, server_cores, throughput). Two stacked segments per bar,
    a 2px surface gap between them, throughput written at the bar end."""
    bar_h, gap = 22, 10
    L, R, T, B = 96, 150, 8, 44
    W = 720
    ph = len(rows) * (bar_h + gap) - gap
    H = T + ph + B
    pw = W - L - R
    x = lambda v: L + pw * v / xmax
    out = [svg_open(W, H)]
    for t in range(0, xmax + 1, 10):
        tx = x(t)
        if t:
            out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{T}" y2="{T + ph}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{T + ph}" y2="{T + ph + 5}" stroke="{AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{tx:.1f}" y="{T + ph + 20}" text-anchor="middle" font-size="12" fill="{MUTED}">{t}</text>')
    for i, (label, cli, srv, thr) in enumerate(rows):
        by = T + i * (bar_h + gap)
        cw = max(x(cli) - L, 1)
        sw = max(x(cli + srv) - x(cli) - 2, 1)
        out.append(f'<rect x="{L}" y="{by}" width="{cw:.1f}" height="{bar_h}" fill="{S1}"/>')
        sx = L + cw + 2
        r = min(4, sw / 2)
        out.append(f'<path d="M{sx:.1f},{by} h{sw - r:.1f} a{r},{r} 0 0 1 {r},{r} v{bar_h - 2 * r} '
                   f'a{r},{r} 0 0 1 {-r},{r} h{-(sw - r):.1f} z" fill="{S2}"/>')
        out.append(f'<text x="{L - 10}" y="{by + bar_h / 2 + 4:.1f}" text-anchor="end" font-size="12" fill="{INK2}">{label}</text>')
        out.append(f'<text x="{sx + sw + 8:.1f}" y="{by + bar_h / 2 + 4:.1f}" font-size="12" fill="{INK2}">'
                   f'{cli + srv:.0f} cores <tspan fill="{MUTED}">· {thr} {unit}</tspan></text>')
    out.append(f'<line x1="{L}" x2="{L}" y1="{T}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<line x1="{L}" x2="{L + pw}" y1="{T + ph}" y2="{T + ph}" stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{H - 6}" text-anchor="middle" font-size="12" fill="{INK2}">'
               f'Cores busy on the 80-CPU machine (average over the whole run)</text>')
    out.append("</svg>")
    return "\n".join(out), H


def cores_rows(workload, threads, langs, fmt):
    rows = []
    for lang in langs:
        r = run(workload, lang, threads)
        wall = r["run_wall_s"]
        rows.append((NAMES[lang], r["client_cpu_s"] / wall, r["server_cpu_s"] / wall, r["throughput"]))
    rows.sort(key=lambda row: -row[3])
    return [(a, b, c, fmt(d)) for a, b, c, d in rows]


def build_post_lowcpu():
    legend = [(None, "Client (benchmark process)", S1), (None, "Database server (mysqld)", S2)]
    rows = cores_rows(2, 512, ["c", "lua", "php", "python", "ruby"], fmt_m)
    svg, h = stacked_cores_svg(rows, "rows/s")
    render("post-lowcpu-w2", page("Integer reads at 512 workers: where the CPU goes", svg, legend),
           height=int(h * 760 / 720) + 90)
    rows = cores_rows(3, 512, ["c", "php", "lua", "go", "csharp", "java", "python"], lambda v: f"{v:.0f}")
    svg, h = stacked_cores_svg(rows, "blobs/s")
    render("post-lowcpu-w3", page("Blob inserts at 512 workers: where the CPU goes", svg, legend),
           height=int(h * 760 / 720) + 90)


def build_post_native():
    """Native languages on blob reads: libmysqlclient (C, Swift) vs own-language drivers (Rust, Go)."""
    series = [
        ("c", "C (native reference)", REF),
        ("swift", "Swift", S1),
        ("rust", "Rust", S2),
        ("go", "Go", S3),
    ]
    svg = line_chart_svg(4, series, ymax=9e3, ytick=1e3, tick_fmt=lambda t: f"{t:,.0f}",
                         end_fmt=lambda v: f"{v:,.0f}", ylabel="Blobs per second", left=62)
    render("post-native-w4", page("Blob reads: 1 MiB blobs read and hashed per second", svg, series))


if __name__ == "__main__":
    build_post1()
    build_post2()
    build_post_managed()
    build_post_lowcpu()
    build_post_native()
