#!/usr/bin/env python3
"""Render the blog chart images from runs/avg.json.

Builds a static SVG line chart for each post and renders it to a 2x PNG in
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
# (lang key, legend label, colour). C is a neutral reference line.
SERIES = [
    ("c", "C (native reference)", "#8a8984"),
    ("lua", "Lua", "#eb6834"),
    ("php", "PHP", "#2a78d6"),
    ("javascript", "JavaScript", "#1baf7a"),
    ("python", "Python", "#eda100"),
    ("ruby", "Ruby", "#e87ba4"),
]


def values(workload, lang):
    by_t = {v["threads"]: v["throughput"] for v in DATA.values()
            if v["workload"] == workload and v["lang"] == lang}
    return [by_t[t] for t in THREADS]


def fmt_m(x):
    return f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.0f}K"


def line_chart_svg(workload, ymax, ytick):
    W, H = 720, 400
    L, R, T, B = 56, 148, 16, 48
    pw, ph = W - L - R, H - T - B
    xs = [L + i * pw / (len(THREADS) - 1) for i in range(len(THREADS))]
    y = lambda v: T + ph * (1 - v / ymax)

    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">']
    # Horizontal gridlines and y tick labels.
    t = 0
    while t <= ymax + 1:
        ty = y(t)
        if t > 0:
            out.append(f'<line x1="{L}" x2="{L + pw}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<line x1="{L - 5}" x2="{L}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{AXIS}" stroke-width="1"/>')
        out.append(f'<text x="{L - 9}" y="{ty + 4:.1f}" text-anchor="end" font-size="12" fill="{MUTED}">{t / 1e6:.0f}M</text>')
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
    out.append(f'<text transform="translate(14 {T + ph / 2:.1f}) rotate(-90)" text-anchor="middle" font-size="12" fill="{INK2}">Rows per second</text>')

    # Lines, an end marker and end labels.
    ends = []
    for lang, label, colour in SERIES:
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
                   f'{name} <tspan fill="{MUTED}">{fmt_m(v)}</tspan></text>')
    out.append("</svg>")
    return "\n".join(out)


def page(title, svg):
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px">'
        f'<i style="display:inline-block;width:14px;height:3px;border-radius:2px;background:{c}"></i>{lbl}</span>'
        for _, lbl, c in SERIES)
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>'
            f'body{{margin:0;background:{BG};font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}'
            f'.wrap{{width:760px;padding:16px 20px}} h1{{font-size:17px;margin:0 0 6px;color:{INK}}}'
            f'.legend{{font-size:13px;color:{INK2};margin-bottom:6px}} svg{{width:100%;height:auto;display:block}}'
            f'</style></head><body><div class="wrap"><h1>{title}</h1><div class="legend">{legend}</div>{svg}</div></body></html>')


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
    svg = line_chart_svg(2, ymax=30e6, ytick=5e6)
    render("post-1-w2", page("Rows read and summed per second, by number of workers", svg))


if __name__ == "__main__":
    build_post1()
