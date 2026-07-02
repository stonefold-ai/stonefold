"""Render the Track-R pilot graph (SVG, no dependencies) from cells.csv files.

Usage: python make_graph.py OUT.svg LABEL=path/to/cells.csv [LABEL=path ...]
Each cells.csv row: condition,n,count,correct,...,tokens_mean (acp_bench schema).
Two panels: correct-selection rate vs N, and mean tokens/call vs N.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

COLORS = {
    ("haiku", "mcp"): "#e07b39", ("haiku", "sif"): "#2b7de9",
    ("sonnet", "mcp"): "#a4501f", ("sonnet", "sif"): "#1a4e94",
    ("opus", "mcp"): "#c22f2f", ("opus", "sif"): "#0e7c4a",
}
DASH = {"haiku": "", "sonnet": "6,3", "opus": "2,2"}

W, H, PAD_L, PAD_R, PAD_T, PAD_B, GAP = 960, 470, 60, 20, 80, 64, 70
PANEL_W = (W - PAD_L - PAD_R - GAP) // 2 - PAD_L // 2


def load(label: str, path: Path) -> dict[tuple[str, int], dict[str, float]]:
    out: dict[tuple[str, int], dict[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[(row["condition"], int(row["n"]))] = {
                "correct": float(row["correct"]), "tokens": float(row["tokens_mean"])}
    return out


def main() -> int:
    out_path = Path(sys.argv[1])
    series: dict[str, dict[tuple[str, int], dict[str, float]]] = {}
    for arg in sys.argv[2:]:
        label, _, p = arg.partition("=")
        cells = load(label, Path(p))
        series.setdefault(label, {}).update(cells)

    ns = sorted({n for cells in series.values() for (_, n) in cells})
    max_tok = max(v["tokens"] for cells in series.values() for v in cells.values())
    max_tok = (int(max_tok / 500) + 1) * 500

    def panel(x0: float, title: str, ymax: float, yfmt, value_key: str) -> list[str]:
        x1, y0, y1 = x0 + PANEL_W, H - PAD_B, PAD_T
        el = [f'<text x="{x0}" y="{PAD_T - 14}" class="t">{title}</text>']
        # axes + gridlines
        for i in range(5):
            y = y0 - (y0 - y1) * i / 4
            el.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" class="grid"/>')
            el.append(f'<text x="{x0 - 6}" y="{y + 4}" class="ax" text-anchor="end">{yfmt(ymax * i / 4)}</text>')
        for n in ns:
            x = x0 + (x1 - x0) * ns.index(n) / max(len(ns) - 1, 1)
            el.append(f'<text x="{x}" y="{y0 + 16}" class="ax" text-anchor="middle">{n}</text>')
        el.append(f'<text x="{(x0 + x1) / 2}" y="{y0 + 32}" class="ax" text-anchor="middle">N (capabilities in the selection space)</text>')
        # series lines
        for label, cells in series.items():
            for cond in ("mcp", "sif"):
                pts = []
                for n in ns:
                    if (cond, n) not in cells:
                        continue
                    x = x0 + (x1 - x0) * ns.index(n) / max(len(ns) - 1, 1)
                    y = y0 - (y0 - y1) * min(cells[(cond, n)][value_key], ymax) / ymax
                    pts.append((x, y))
                if not pts:
                    continue
                color = COLORS.get((label, cond), "#666")
                dash = f' stroke-dasharray="{DASH.get(label, "")}"' if DASH.get(label) else ""
                d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
                el.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.5"{dash}/>')
                for x, y in pts:
                    el.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
        return el

    legend = []
    lx = PAD_L
    for label, cells in series.items():
        for cond in ("mcp", "sif"):
            if not any(c == cond for (c, _) in cells):
                continue
            color = COLORS.get((label, cond), "#666")
            legend.append(f'<rect x="{lx}" y="{H - 18}" width="14" height="4" fill="{color}"/>')
            legend.append(f'<text x="{lx + 18}" y="{H - 12}" class="ax">{label} · {cond}</text>')
            lx += 130

    body = "\n".join(
        panel(PAD_L, "Correct capability selection", 1.0, lambda v: f"{v * 100:.0f}%", "correct")
        + panel(PAD_L + PANEL_W + GAP, "Mean tokens / call", max_tok, lambda v: f"{v:.0f}", "tokens")
        + legend
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<style>
 .t {{ font: 600 15px sans-serif; fill: #222; }}
 .st {{ font: 12px sans-serif; fill: #555; }}
 .ax {{ font: 11px sans-serif; fill: #555; }}
 .grid {{ stroke: #ddd; stroke-width: 1; }}
</style>
<rect width="{W}" height="{H}" fill="white"/>
<text x="{PAD_L}" y="20" class="t">Track R pilot — MCP tool surface vs SIF submit_intent (real models, 2 reps × 10 probes/cell)</text>
<text x="{PAD_L}" y="36" class="st">PILOT — below the docs/15 §5 bar (2 reps, estimated tokens). Raw logs: bench_results/. Fixed-surface runs only.</text>
{body}
</svg>'''
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
