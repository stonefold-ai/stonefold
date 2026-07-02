"""Render the Track-R realism-battery graph (SVG, no dependencies) from cells.csv
files.

Usage:
    python make_graph.py OUT.svg CONFIG=path/to/cells.csv [CONFIG=path ...]

Each CONFIG label becomes one bar group (in argument order); the special label
``no-tool`` is the distractor run (its "correct" = answered WITHOUT calling a tool).
Panels: correct-selection bars at N=10 and N=50 per configuration, and a tokens
panel comparing terse vs realistic tool cards (built from the ``confusable`` and
``realistic`` configs when both are present). The earlier count-sweep layout this
replaces is in git history (superseded with its runs; see README).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

MCP_COLOR = "#e07b39"
SIF_COLOR = "#2b7de9"

W, H = 960, 500
PANEL_X0S = (60, 388)             # N=10 panel, N=50 panel
PANEL_W = 300
TOK_X0, TOK_W = 736, 190
Y0, Y1 = 356, 110                 # plot area (bottom, top)
NS = (10, 50)


def load(path: Path) -> dict[tuple[str, int], dict[str, float]]:
    out: dict[tuple[str, int], dict[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[(row["condition"], int(row["n"]))] = {
                "correct": float(row["correct"]), "tokens": float(row["tokens_mean"])}
    return out


def main() -> int:
    out_path = Path(sys.argv[1])
    configs: list[str] = []
    data: dict[str, dict[tuple[str, int], dict[str, float]]] = {}
    for arg in sys.argv[2:]:
        label, _, p = arg.partition("=")
        if label not in data:
            data[label] = {}
            configs.append(label)
        data[label].update(load(Path(p)))

    el: list[str] = []

    def pct_y(v: float) -> float:
        return Y0 - (Y0 - Y1) * v

    # --- two bar panels: correct selection per configuration, N=10 and N=50 --
    for pi, n in enumerate(NS):
        x0 = PANEL_X0S[pi]
        x1 = x0 + PANEL_W
        el.append(f'<text x="{x0}" y="{Y1 - 24}" class="t">Right capability picked — '
                  f'{n} to choose from</text>')
        for pct in (0, 25, 50, 75, 100):
            y = pct_y(pct / 100)
            el.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>')
            if pi == 0:
                el.append(f'<text x="{x0 - 6}" y="{y + 3.5:.1f}" class="ax" text-anchor="end">{pct}%</text>')
        group_w = (PANEL_W - 16) / max(len(configs), 1)
        for gi, cfg in enumerate(configs):
            gx = x0 + 8 + group_w * gi + group_w / 2
            el.append(f'<text x="{gx:.1f}" y="{Y0 + 14}" class="axs" text-anchor="middle">{cfg}</text>')
            vals = {cond: data[cfg][(cond, n)]["correct"]
                    for cond in ("mcp", "sif") if (cond, n) in data[cfg]}
            for cond, color, dx in (("mcp", MCP_COLOR, -15), ("sif", SIF_COLOR, 1)):
                if cond not in vals:
                    continue
                y = pct_y(vals[cond])
                el.append(f'<rect x="{gx + dx:.1f}" y="{y:.1f}" width="14" '
                          f'height="{Y0 - y:.1f}" fill="{color}"/>')
            if len(vals) == 2 and vals["mcp"] == vals["sif"]:
                el.append(f'<text x="{gx:.1f}" y="{pct_y(vals["mcp"]) - 4:.1f}" class="vl" '
                          f'text-anchor="middle">{vals["mcp"] * 100:.0f}</text>')
            else:
                for cond, dx in (("mcp", -15), ("sif", 1)):
                    if cond in vals:
                        el.append(f'<text x="{gx + dx + 7:.1f}" y="{pct_y(vals[cond]) - 4:.1f}" '
                                  f'class="vl" text-anchor="middle">{vals[cond] * 100:.0f}</text>')

    # --- tokens panel: terse vs realistic tool cards (N=50) ------------------
    terse = data.get("confusable", {})
    rich = data.get("realistic", {})
    bars = [(f"{cond}-{card}", src[(cond, 50)]["tokens"],
             MCP_COLOR if cond == "mcp" else SIF_COLOR)
            for card, src in (("terse", terse), ("real", rich))
            for cond in ("mcp", "sif") if (cond, 50) in src]
    if bars:
        ymax_tok = (int(max(v for _, v, _ in bars) / 500) + 1) * 500

        def tok_y(v: float) -> float:
            return Y0 - (Y0 - Y1) * v / ymax_tok

        el.append(f'<text x="{TOK_X0}" y="{Y1 - 24}" class="t">Tokens per call</text>')
        el.append(f'<text x="{TOK_X0}" y="{Y1 - 10}" class="st">N=50; terse vs realistic tool cards</text>')
        for v in range(0, ymax_tok + 1, 1000):
            y = tok_y(v)
            el.append(f'<line x1="{TOK_X0}" y1="{y:.1f}" x2="{TOK_X0 + TOK_W}" y2="{y:.1f}" class="grid"/>')
            el.append(f'<text x="{TOK_X0 - 6}" y="{y + 3.5:.1f}" class="ax" text-anchor="end">{v}</text>')
        bw = (TOK_W - 20) / max(len(bars), 1)
        for bi, (blabel, v, color) in enumerate(bars):
            bx = TOK_X0 + 10 + bw * bi
            y = tok_y(v)
            el.append(f'<rect x="{bx + 4:.1f}" y="{y:.1f}" width="{bw - 8:.1f}" '
                      f'height="{Y0 - y:.1f}" fill="{color}"/>')
            el.append(f'<text x="{bx + bw / 2:.1f}" y="{y - 4:.1f}" class="vl" '
                      f'text-anchor="middle">{v:.0f}</text>')
            el.append(f'<text x="{bx + bw / 2:.1f}" y="{Y0 + 14}" class="axs" '
                      f'text-anchor="middle">{blabel}</text>')

    # --- captions + legend ----------------------------------------------------
    el.append(f'<text x="60" y="{Y0 + 40}" class="cap">Bars: out of 20 attempts each (10 tasks × 2 repeats, Claude Haiku 4.5), how often the model called the capability the task needed —</text>')
    el.append(f'<text x="60" y="{Y0 + 56}" class="cap">with look-alike capabilities in the selection space (confusable), different prompt wordings (explicit / vague), ~2,000 tokens of prior conversation</text>')
    el.append(f'<text x="60" y="{Y0 + 72}" class="cap">(context2k), and production-length tool descriptions (realistic). "no-tool" prompts need NO call — that bar is how often the model correctly stayed quiet.</text>')
    ly = H - 20
    el.append(f'<rect x="60" y="{ly - 5}" width="14" height="10" fill="{MCP_COLOR}"/>')
    el.append(f'<text x="80" y="{ly + 4}" class="ax">MCP — the same N capabilities as N separate tools</text>')
    el.append(f'<rect x="380" y="{ly - 5}" width="14" height="10" fill="{SIF_COLOR}"/>')
    el.append(f'<text x="400" y="{ly + 4}" class="ax">SIF — one submit_intent tool declaring all N</text>')

    body = "\n".join(el)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<style>
 .t {{ font: 600 13px sans-serif; fill: #222; }}
 .st {{ font: 11px sans-serif; fill: #777; }}
 .ax {{ font: 11px sans-serif; fill: #555; }}
 .axs {{ font: 9px sans-serif; fill: #555; }}
 .vl {{ font: 9px sans-serif; fill: #444; }}
 .cap {{ font: 11px sans-serif; fill: #444; }}
 .grid {{ stroke: #e3e3e3; stroke-width: 1; }}
</style>
<rect width="{W}" height="{H}" fill="white"/>
<text x="60" y="24" class="t" style="font-size:15px">Closer to real life: look-alike tools, vague requests, long context — MCP tool surface vs one SIF intent tool</text>
<text x="60" y="41" class="st">Claude Haiku 4.5, real API calls. Early pilot: small sample (2 repeats), token counts estimated. Raw logs and per-run reports: bench_results/.</text>
{body}
</svg>'''
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
