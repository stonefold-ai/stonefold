"""One-command runner for the benchmark harness (docs/15 §5).

    python -m acp_bench --smoke                 # deterministic fake LLM, proves it runs
    python -m acp_bench --run --models small,mid --reps 5 --out DIR   # the author's run

``--smoke`` drives the fake LLM over the wired rungs (S0 + S3) and the wired attack,
prints a matrix under a loud SMOKE banner, and is safe to run with no API key. ``--run``
executes the real experiment — that, its baselines' fairness, and publication are the
author's, personally (docs/15 §6–§7); this tool never publishes a number for you.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from acp_bench.attacks import ATTACKS
from acp_bench.conditions import CONDITIONS
from acp_bench.harness import run_security, unconfigured_rungs, unwired_attacks
from acp_bench.model import PINNED_MODELS, ModelSpec, model_by_key
from acp_bench.report import ReportMeta, render
from acp_bench.runner import MIN_REPS


def _banner() -> str:
    return __doc__ or ""


def _smoke(out_dir: Path | None) -> int:
    models: tuple[ModelSpec, ...] = (model_by_key("fake"),)
    reps = 2
    matrix, trials = run_security(models, CONDITIONS, ATTACKS, reps=reps, out_dir=out_dir)
    meta = ReportMeta(
        smoke=True,
        models=tuple(m.key for m in models),
        reps=reps,
        unconfigured_rungs=unconfigured_rungs(CONDITIONS),
        unwired_attacks=unwired_attacks(ATTACKS),
    )
    print(render(matrix, meta))
    print(f"\n[smoke] {len(trials)} trials over the fake LLM. NOT A RESULT (docs/15 §6).")
    if out_dir is not None:
        print(f"[smoke] raw log: {out_dir / 'security_trials.jsonl'}")
    return 0


def _run(model_keys: list[str], reps: int, out_dir: Path | None) -> int:
    models = tuple(model_by_key(k) for k in model_keys)
    matrix, trials = run_security(models, CONDITIONS, ATTACKS, reps=reps, out_dir=out_dir)
    meta = ReportMeta(
        smoke=False,
        models=tuple(m.key for m in models),
        reps=reps,
        unconfigured_rungs=unconfigured_rungs(CONDITIONS),
        unwired_attacks=unwired_attacks(ATTACKS),
    )
    print(render(matrix, meta))
    print(f"\n{len(trials)} trials. Publish only with the harness, configs, and raw logs (§6).")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The report is Unicode Markdown (—, §, ×). Force UTF-8 stdout so it prints
    # correctly on a cp1252 Windows console (guarded: not every stream supports it).
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(
        prog="acp_bench", description="Benchmark harness (docs/15) — build only.",
        epilog="Available model keys: " + ", ".join(m.key for m in PINNED_MODELS),
    )
    parser.add_argument("--smoke", action="store_true", help="run the fake-LLM smoke test")
    parser.add_argument("--run", action="store_true", help="run the real experiment (author)")
    parser.add_argument("--models", default="", help="comma-separated pinned model keys")
    parser.add_argument("--reps", type=int, default=MIN_REPS, help=f"repetitions/cell (>= {MIN_REPS})")
    parser.add_argument("--out", default="", help="directory for raw logs")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else None

    if args.smoke:
        return _smoke(out_dir)
    if args.run:
        keys = [k.strip() for k in args.models.split(",") if k.strip()]
        if not keys:
            parser.error("--run needs --models (e.g. --models small,mid)")
        return _run(keys, args.reps, out_dir)

    # no action: show the honesty banner + usage, do nothing.
    print(_banner())
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
