"""Render the Track-R pilot graph (SVG, no dependencies) from cells.csv files.

Usage: python make_graph.py OUT.svg LABEL=path/to/cells.csv [LABEL=path ...]
Each cells.csv row: condition,n,count,correct,...,tokens_mean (acp_bench schema).

Design notes (the data largely coincides, so the chart types are chosen to make the
ties readable instead of overlapping):

* Selection accuracy — grouped BARS, one small panel per model: bars cannot paint
  over each other, equal bars read as the tie they are, and every bar carries its
  value label.
* Tokens per call — LINES (the story is growth: linear for MCP, sub-linear for SIF),
  one line per surface averaged over the three models (they agree within ~1%; the
  per-model markers show it).
* All in-image text is plain language; spec references stay in the README/docs.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

MCP_COLOR = "#e07b39"
SIF_COLOR = "#2b7de9"
MARKERS = ("circle", "square", "triangle")  # per model, in first-seen order

W, H = 960, 470
SEL_X0S = (60, 262, 464)          # three mini bar panels, one per model
SEL_W = 168
TOK_X0, TOK_W = 700, 226
Y0, Y1 = 342, 104                 # plot area (bottom, top)


def load(path: Path) -> dict[tuple[str, int], dict[str, float]]:
    out: dict[tuple[str, int], dict[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[(row["condition"], int(row["n"]))] = {
                "correct": float(row["correct"]), "tokens": float(row["tokens_mean"])}
    return out


def marker(kind: str, x: float, y: float, color: str) -> str:
    if kind == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{color}"/>'
    if kind == "square":
        return f'<rect x="{x - 3.2:.1f}" y="{y - 3.2:.1f}" width="6.4" height="6.4" fill="{color}"/>'
    return (f'<path d="M{x:.1f},{y - 4.2:.1f} L{x + 4:.1f},{y + 3.2:.1f} '
            f'L{x - 4:.1f},{y + 3.2:.1f} Z" fill="{color}"/>')


def main() -> int:
    out_path = Path(sys.argv[1])
    series: dict[str, dict[tuple[str, int], dict[str, float]]] = {}
    order: list[str] = []
    for arg in sys.argv[2:]:
        label, _, p = arg.partition("=")
        if label not in series:
            series[label] = {}
            order.append(label)
        series[label].update(load(Path(p)))

    ns = sorted({n for cells in series.values() for (_, n) in cells})
    el: list[str] = []

    # --- selection accuracy: grouped bars, one mini panel per model ---------
    def pct_y(v: float) -> float:
        return Y0 - (Y0 - Y1) * v

    el.append(f'<text x="{SEL_X0S[0]}" y="72" class="t">How often the model picked the right capability</text>')
    for i, label in enumerate(order[: len(SEL_X0S)]):
        x0 = SEL_X0S[i]
        x1 = x0 + SEL_W
        el.append(f'<text x="{(x0 + x1) / 2:.0f}" y="{Y1 - 18}" class="pt" text-anchor="middle">{label}</text>')
        for pct in (0, 25, 50, 75, 100):
            y = pct_y(pct / 100)
            el.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>')
            if i == 0:
                el.append(f'<text x="{x0 - 6}" y="{y + 3.5:.1f}" class="ax" text-anchor="end">{pct}%</text>')
        cells = series[label]
        group_w = (SEL_W - 20) / len(ns)
        for gi, n in enumerate(ns):
            gx = x0 + 10 + group_w * gi + group_w / 2
            el.append(f'<text x="{gx:.1f}" y="{Y0 + 15}" class="ax" text-anchor="middle">{n}</text>')
            vals = {cond: cells[(cond, n)]["correct"]
                    for cond in ("mcp", "sif") if (cond, n) in cells}
            for cond, color, dx in (("mcp", MCP_COLOR, -17), ("sif", SIF_COLOR, 2)):
                if cond not in vals:
                    continue
                y = pct_y(vals[cond])
                el.append(f'<rect x="{gx + dx:.1f}" y="{y:.1f}" width="15" '
                          f'height="{Y0 - y:.1f}" fill="{color}"/>')
            if len(vals) == 2 and vals["mcp"] == vals["sif"]:
                # equal pair: one centered label above says it for both bars
                v = vals["mcp"]
                el.append(f'<text x="{gx:.1f}" y="{pct_y(v) - 4:.1f}" class="vl" '
                          f'text-anchor="middle">{v * 100:.0f}</text>')
            else:
                # unequal bars differ in height, so above-bar labels cannot collide
                for cond, dx in (("mcp", -17), ("sif", 2)):
                    if cond not in vals:
                        continue
                    v = vals[cond]
                    el.append(f'<text x="{gx + dx + 7.5:.1f}" y="{pct_y(v) - 4:.1f}" class="vl" '
                              f'text-anchor="middle">{v * 100:.0f}</text>')

    # --- tokens per call: two mean lines + per-model markers ----------------
    max_tok = max(v["tokens"] for cells in series.values() for v in cells.values())
    ymax = (int(max_tok / 500) + 1) * 500

    def tok_y(v: float) -> float:
        return Y0 - (Y0 - Y1) * v / ymax

    def tok_x(n: int) -> float:
        return TOK_X0 + 14 + (TOK_W - 28) * ns.index(n) / max(len(ns) - 1, 1)

    el.append(f'<text x="{TOK_X0}" y="72" class="t">Tokens per call</text>')
    el.append(f'<text x="{(TOK_X0 + TOK_X0 + TOK_W) / 2:.0f}" y="{Y1 - 18}" class="pt" '
              f'text-anchor="middle">all three models (±1%)</text>')
    for v in range(0, ymax + 1, 500):
        y = tok_y(v)
        el.append(f'<line x1="{TOK_X0}" y1="{y:.1f}" x2="{TOK_X0 + TOK_W}" y2="{y:.1f}" class="grid"/>')
        el.append(f'<text x="{TOK_X0 - 6}" y="{y + 3.5:.1f}" class="ax" text-anchor="end">{v}</text>')
    for n in ns:
        el.append(f'<text x="{tok_x(n):.1f}" y="{Y0 + 15}" class="ax" text-anchor="middle">{n}</text>')
    mean_at: dict[tuple[str, int], float] = {}
    for cond, color in (("mcp", MCP_COLOR), ("sif", SIF_COLOR)):
        pts = []
        for n in ns:
            vals = [series[m][(cond, n)]["tokens"] for m in order if (cond, n) in series[m]]
            if not vals:
                continue
            mean_at[(cond, n)] = sum(vals) / len(vals)
            pts.append((tok_x(n), tok_y(mean_at[(cond, n)])))
        d = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y) in enumerate(pts))
        el.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.4"/>')
        for mi, m in enumerate(order):
            for n in ns:
                if (cond, n) in series[m]:
                    el.append(marker(MARKERS[mi % 3], tok_x(n), tok_y(series[m][(cond, n)]["tokens"]), color))
    n_last = ns[-1]
    if ("mcp", n_last) in mean_at and ("sif", n_last) in mean_at and mean_at[("sif", n_last)]:
        ratio = mean_at[("mcp", n_last)] / mean_at[("sif", n_last)]
        el.append(f'<text x="{tok_x(n_last) - 8:.1f}" y="{tok_y(mean_at[("mcp", n_last)]) + 18:.1f}" '
                  f'class="pt" text-anchor="end" fill="{MCP_COLOR}">MCP</text>')
        el.append(f'<text x="{tok_x(n_last) - 8:.1f}" y="{tok_y(mean_at[("sif", n_last)]) - 8:.1f}" '
                  f'class="pt" text-anchor="end" fill="{SIF_COLOR}">SIF</text>')
        ymid = (tok_y(mean_at[("mcp", n_last)]) + tok_y(mean_at[("sif", n_last)])) / 2
        el.append(f'<text x="{tok_x(n_last) - 8:.1f}" y="{ymid:.1f}" class="pt" '
                  f'text-anchor="end">{ratio:.1f}× cheaper</text>')

    # --- shared x-label, plain-language captions, legend ---------------------
    el.append(f'<text x="{W / 2}" y="{Y0 + 33}" class="ax" text-anchor="middle">'
              'N = how many capabilities the model can choose from</text>')
    el.append(f'<text x="60" y="404" class="cap">Left: out of 20 attempts per bar (10 tasks × 2 repeats), '
              'how often the model called the capability the task needed. Equal bars = both ways worked equally well.</text>')
    el.append(f'<text x="60" y="420" class="cap">Right: an MCP agent resends all N tool definitions with every call; '
              'SIF sends one tool whose list of names grows. Lines are the 3-model average; markers are the individual models.</text>')
    ly = H - 22
    el.append(f'<rect x="60" y="{ly - 5}" width="14" height="10" fill="{MCP_COLOR}"/>')
    el.append(f'<text x="80" y="{ly + 4}" class="ax">MCP — the same N capabilities as N separate tools</text>')
    el.append(f'<rect x="368" y="{ly - 5}" width="14" height="10" fill="{SIF_COLOR}"/>')
    el.append(f'<text x="388" y="{ly + 4}" class="ax">SIF — one submit_intent tool declaring all N</text>')
    lx = 700
    for mi, m in enumerate(order):
        el.append(marker(MARKERS[mi % 3], lx, ly, "#555"))
        el.append(f'<text x="{lx + 9}" y="{ly + 4}" class="ax">{m}</text>')
        lx += 78

    body = "\n".join(el)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<style>
 .t {{ font: 600 14px sans-serif; fill: #222; }}
 .pt {{ font: 600 12px sans-serif; fill: #333; }}
 .st {{ font: 11px sans-serif; fill: #777; }}
 .ax {{ font: 11px sans-serif; fill: #555; }}
 .vl {{ font: 9px sans-serif; fill: #444; }}
 .cap {{ font: 11px sans-serif; fill: #444; }}
 .grid {{ stroke: #e3e3e3; stroke-width: 1; }}
</style>
<rect width="{W}" height="{H}" fill="white"/>
<text x="60" y="24" class="t">Giving a model N capabilities: as N separate MCP tools, or as one SIF intent tool?</text>
<text x="60" y="41" class="st">Same tasks, same models (Claude Haiku 4.5, Sonnet 5, Opus 4.8), real API calls. Early pilot: small sample (2 repeats), token counts estimated.</text>
{body}
</svg>'''
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
