# SPDX-License-Identifier: Apache-2.0
"""Console runner for the benchmark harness (docs/15 §5). Two tracks, each runnable
in isolation:

    # Track R — tool-selection effectiveness (MCP vs retrieval-MCP vs SIF):
    python -m stonefold_bench --track r --smoke
    python -m stonefold_bench --track r --run --models small --reps 5
    python -m stonefold_bench --track r --smoke --surfaces mcp,sif --ns 10,100 --probes pay-invoice

    # Track S — security ablation S0→S3:
    python -m stonefold_bench --track s --smoke
    python -m stonefold_bench --track s --run --models small,mid --reps 5 --rungs S0,S3

Every run streams its output — nothing waits for the end:

    <out>/track-<t>/trials.jsonl   appended + flushed as EACH TRIAL finishes
    <out>/track-<t>/cells.json     aggregated cells, REWRITTEN after every round (rep)
    <out>/track-<t>/cells.csv      the same cells as CSV (graphing convenience)
    <out>/track-<t>/bts.csv        Track S only: benign-task success per rung
    <out>/track-<t>/report.md      final human-readable matrix
    <out>/track-<t>/meta.json      run parameters + start/finish timestamps

``--smoke`` drives the deterministic fake LLM (no API key) and prints a loud SMOKE
banner — it proves the machinery, it is never a result. ``--run`` executes the real
experiment with pinned models; that, its baselines' fairness, and publication are the
author's, personally (docs/15 §6-§7).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stonefold_bench.attacks import ATTACKS
from stonefold_bench.conditions import CONDITIONS, Condition
from stonefold_bench.harness import run_security, unconfigured_rungs, unwired_attacks
from stonefold_bench.matrix import aggregate
from stonefold_bench.model import PINNED_MODELS, ModelSpec, model_by_key
from stonefold_bench.raw_log import JsonlWriter, write_csv, write_json
from stonefold_bench.reliability import (
    CARDS,
    CONDITIONS as R_SURFACES,
    DISTRACTOR_PROBES,
    FILLERS,
    PROBES,
    Probe,
    RTrial,
    cells_as_dicts,
    reliability_matrix,
    render_reliability,
    run_reliability,
)
from stonefold_bench.report import ReportMeta, render
from stonefold_bench.runner import MIN_REPS, Trial
from stonefold_bench.tracks import TOOL_COUNTS

DEFAULT_OUT = "bench_out"


def _utf8_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr)


def _meta(out: Path, base: dict[str, Any], **updates: Any) -> dict[str, Any]:
    base.update(updates)
    write_json(out / "meta.json", base)
    return base


# --- Track R — tool-selection effectiveness --------------------------------
def _run_track_r(models: tuple[ModelSpec, ...], surfaces: tuple[str, ...],
                 ns: tuple[int, ...], probes: tuple[Probe, ...], reps: int,
                 out: Path, *, smoke: bool, fillers: str = "synthetic",
                 cards: str = "terse", phrasing: str = "typical",
                 context_tokens: int = 0) -> int:
    meta = _meta(out, {
        "track": "r", "smoke": smoke, "models": [m.key for m in models],
        "surfaces": list(surfaces), "ns": list(ns), "probes": [p.id for p in probes],
        "reps": reps, "fillers": fillers, "cards": cards, "phrasing": phrasing,
        "context_tokens": context_tokens, "started": _now(), "finished": None,
    })

    def write_cells(trials: list[RTrial], rounds_done: int) -> None:
        rows = cells_as_dicts(reliability_matrix(trials))
        write_json(out / "cells.json", {**{k: meta[k] for k in
                   ("track", "smoke", "models", "reps")}, "rounds_done": rounds_done,
                   "cells": rows})
        write_csv(out / "cells.csv", rows)

    def on_round(rep: int, trials: list[RTrial]) -> None:
        write_cells(trials, rep + 1)
        _progress(f"[track r] round {rep + 1}/{reps} done - {len(trials)} trials - "
                  f"updated {out / 'cells.json'}")

    _progress(f"[track r] streaming trials to {out / 'trials.jsonl'}")
    with JsonlWriter(out / "trials.jsonl") as log:
        trials = run_reliability(
            models, ns, conditions=surfaces, probes=probes, reps=reps,
            fillers=fillers, cards=cards, phrasing=phrasing,
            context_tokens=context_tokens,
            on_trial=lambda t: log.write(t.as_dict()), on_round=on_round,
        )

    report = render_reliability(reliability_matrix(trials),
                                models=tuple(m.key for m in models), reps=reps,
                                smoke=smoke, probe_count=len(probes))
    (out / "report.md").write_text(report, encoding="utf-8", newline="\n")
    _meta(out, meta, finished=_now(), trials=len(trials))
    print(report)
    tag = "[smoke] " if smoke else ""
    print(f"\n{tag}{len(trials)} reliability trials -> {out}"
          + (" NOT A RESULT." if smoke else " Publish only with logs (docs/15 §6)."))
    return 0


# --- Track S — security ablation --------------------------------------------
def _run_track_s(models: tuple[ModelSpec, ...], conditions: tuple[Condition, ...],
                 reps: int, out: Path, *, smoke: bool) -> int:
    meta = _meta(out, {
        "track": "s", "smoke": smoke, "models": [m.key for m in models],
        "rungs": [c.rung.value for c in conditions], "reps": reps,
        "unconfigured_rungs": list(unconfigured_rungs(conditions)),
        "unwired_attacks": list(unwired_attacks(ATTACKS)),
        "started": _now(), "finished": None,
    })

    def write_cells(trials: list[Trial], rounds_done: int) -> None:
        matrix = aggregate(trials)
        bundle = matrix.as_dict()
        write_json(out / "cells.json", {**{k: meta[k] for k in
                   ("track", "smoke", "models", "reps", "unconfigured_rungs",
                    "unwired_attacks")}, "rounds_done": rounds_done, **bundle})
        write_csv(out / "cells.csv", list(bundle["cells"]))
        write_csv(out / "bts.csv",
                  [{"rung": r, "bts": matrix.bts[r], "n": matrix.bts_n[r]}
                   for r in sorted(matrix.bts)])

    def on_round(rep: int, trials: list[Trial]) -> None:
        write_cells(trials, rep + 1)
        _progress(f"[track s] round {rep + 1}/{reps} done - {len(trials)} trials - "
                  f"updated {out / 'cells.json'}")

    _progress(f"[track s] streaming trials to {out / 'trials.jsonl'}")
    with JsonlWriter(out / "trials.jsonl") as log:
        matrix, trials = run_security(
            models, conditions, ATTACKS, reps=reps,
            on_trial=lambda t: log.write(t.as_dict()), on_round=on_round,
        )

    report = render(matrix, ReportMeta(
        smoke=smoke, models=tuple(m.key for m in models), reps=reps,
        unconfigured_rungs=unconfigured_rungs(conditions),
        unwired_attacks=unwired_attacks(ATTACKS),
    ))
    (out / "report.md").write_text(report, encoding="utf-8", newline="\n")
    _meta(out, meta, finished=_now(), trials=len(trials))
    print(report)
    tag = "[smoke] " if smoke else ""
    print(f"\n{tag}{len(trials)} trials -> {out}"
          + (" NOT A RESULT (docs/15 §6)." if smoke else " Publish only with logs (§6)."))
    return 0


# --- CLI ---------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="stonefold_bench", description="Benchmark harness (docs/15). Each track runs "
        "in isolation; output is streamed per trial and per round.",
        epilog="Model keys: " + ", ".join(m.key for m in PINNED_MODELS),
    )
    parser.add_argument("--track", choices=("s", "r"), default="s",
                        help="s = security ablation (default); r = tool-selection "
                             "reliability (MCP vs retrieval vs SIF)")
    parser.add_argument("--smoke", action="store_true", help="run the fake-LLM smoke test")
    parser.add_argument("--run", action="store_true", help="run the real experiment (author)")
    parser.add_argument("--models", default="", help="comma-separated pinned model keys")
    parser.add_argument("--reps", type=int, default=MIN_REPS,
                        help=f"repetitions (rounds) per cell (>= {MIN_REPS} for a run)")
    parser.add_argument("--ns", default=",".join(str(n) for n in TOOL_COUNTS),
                        help="[track r] tool-count sweep, comma-separated")
    parser.add_argument("--surfaces", default=",".join(R_SURFACES),
                        help="[track r] surfaces to run, comma-separated "
                             f"(default: {','.join(R_SURFACES)})")
    parser.add_argument("--probes", default="",
                        help="[track r] probe ids to run (default: all; see README)")
    parser.add_argument("--probe-set", choices=("main", "distractor"), default="main",
                        help="[track r] main = the capability probes; distractor = "
                             "no-tool prompts (a correct model answers WITHOUT calling)")
    parser.add_argument("--fillers", choices=FILLERS, default="synthetic",
                        help="[track r] filler capabilities: synthetic (distant) or "
                             "confusable (near-duplicates of the anchors)")
    parser.add_argument("--cards", choices=CARDS, default="terse",
                        help="[track r] tool cards: terse one-liners or realistic "
                             "(long descriptions + typed params, both surfaces)")
    parser.add_argument("--phrasing", choices=("typical", "explicit", "vague"),
                        default="typical", help="[track r] prompt wording variant")
    parser.add_argument("--context-tokens", type=int, default=0,
                        help="[track r] ~tokens of deterministic conversation history "
                             "prepended before the probe (0 = fresh context)")
    parser.add_argument("--rungs", default="",
                        help="[track s] rungs to run, e.g. S0,S3 (default: all)")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"output base directory (default: {DEFAULT_OUT}/)")
    args = parser.parse_args(argv)

    if not args.smoke and not args.run:
        print(__doc__ or "")
        parser.print_help()
        return 0

    # models: smoke pins the fake; --run needs explicit keys
    if args.smoke:
        models: tuple[ModelSpec, ...] = (model_by_key("fake"),)
        reps = args.reps if args.run else min(args.reps, 2)
    else:
        keys = [k.strip() for k in args.models.split(",") if k.strip()]
        if not keys:
            parser.error("--run needs --models (e.g. --models small)")
        models = tuple(model_by_key(k) for k in keys)
        reps = args.reps

    out = Path(args.out) / f"track-{args.track}"
    out.mkdir(parents=True, exist_ok=True)

    if args.track == "r":
        ns = tuple(int(x) for x in args.ns.split(",") if x.strip())
        surfaces = tuple(s.strip() for s in args.surfaces.split(",") if s.strip())
        for s in surfaces:
            if s not in R_SURFACES:
                parser.error(f"unknown surface {s!r}; known: {', '.join(R_SURFACES)}")
        pool = DISTRACTOR_PROBES if args.probe_set == "distractor" else PROBES
        if args.probes:
            wanted = {p.strip() for p in args.probes.split(",") if p.strip()}
            unknown = wanted - {p.id for p in pool}
            if unknown:
                parser.error(f"unknown probes {sorted(unknown)}; "
                             f"known: {', '.join(p.id for p in pool)}")
            probes = tuple(p for p in pool if p.id in wanted)
        else:
            probes = pool
        if args.context_tokens < 0:
            parser.error("--context-tokens must be >= 0")
        return _run_track_r(models, surfaces, ns, probes, reps, out, smoke=args.smoke,
                            fillers=args.fillers, cards=args.cards,
                            phrasing=args.phrasing, context_tokens=args.context_tokens)

    if args.rungs:
        wanted_rungs = {r.strip().upper() for r in args.rungs.split(",") if r.strip()}
        known = {c.rung.value for c in CONDITIONS}
        unknown = wanted_rungs - known
        if unknown:
            parser.error(f"unknown rungs {sorted(unknown)}; known: {', '.join(sorted(known))}")
        conditions = tuple(c for c in CONDITIONS if c.rung.value in wanted_rungs)
    else:
        conditions = CONDITIONS
    return _run_track_s(models, conditions, reps, out, smoke=args.smoke)


if __name__ == "__main__":
    sys.exit(main())
