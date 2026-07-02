"""One-command runner for the benchmark harness (docs/15 §5).

    # Track S (security ablation S0->S3):
    python -m acp_bench --smoke
    python -m acp_bench --run --models small,mid --reps 5 --out DIR

    # Track R (reliability vs. tool count):
    python -m acp_bench --track r --smoke
    python -m acp_bench --track r --run --models small --reps 5 --out DIR

``--smoke`` drives the deterministic fake LLM (no API key) and prints a matrix under a
loud SMOKE banner. ``--run`` executes the real experiment with pinned models — that, its
baselines' fairness, and publication are the author's, personally (docs/15 §6-§7).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from acp_bench.attacks import ATTACKS
from acp_bench.conditions import CONDITIONS
from acp_bench.harness import run_security, unconfigured_rungs, unwired_attacks
from acp_bench.model import PINNED_MODELS, ModelSpec, model_by_key
from acp_bench.reliability import (
    RTrial,
    reliability_matrix,
    render_reliability,
    run_reliability,
)
from acp_bench.report import ReportMeta, render
from acp_bench.runner import MIN_REPS
from acp_bench.tracks import TOOL_COUNTS


def _utf8_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


# --- Track S --------------------------------------------------------------
def _security(models: tuple[ModelSpec, ...], reps: int, out_dir: Path | None,
              *, smoke: bool) -> int:
    matrix, trials = run_security(models, CONDITIONS, ATTACKS, reps=reps, out_dir=out_dir)
    meta = ReportMeta(
        smoke=smoke, models=tuple(m.key for m in models), reps=reps,
        unconfigured_rungs=unconfigured_rungs(CONDITIONS),
        unwired_attacks=unwired_attacks(ATTACKS),
    )
    print(render(matrix, meta))
    tag = "[smoke] " if smoke else ""
    print(f"\n{tag}{len(trials)} trials."
          + (" NOT A RESULT (docs/15 §6)." if smoke else " Publish only with logs (§6)."))
    return 0


# --- Track R --------------------------------------------------------------
def _write_rtrials(out_dir: Path, trials: list[RTrial]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reliability_trials.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for t in trials:
            fh.write(json.dumps(t.as_dict(), sort_keys=True) + "\n")
    return path


def _reliability(models: tuple[ModelSpec, ...], ns: tuple[int, ...], reps: int,
                 out_dir: Path | None, *, smoke: bool) -> int:
    trials = run_reliability(models, ns, reps=reps)
    cells = reliability_matrix(trials)
    report = render_reliability(cells, models=tuple(m.key for m in models), reps=reps, smoke=smoke)
    print(report)
    if out_dir is not None:
        log = _write_rtrials(out_dir, trials)
        (out_dir / "reliability_report.md").write_text(report, encoding="utf-8", newline="\n")
        print(f"\nraw log: {log}", file=sys.stderr)
    tag = "[smoke] " if smoke else ""
    print(f"\n{tag}{len(trials)} reliability trials."
          + (" NOT A RESULT." if smoke else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="acp_bench", description="Benchmark harness (docs/15).",
        epilog="Model keys: " + ", ".join(m.key for m in PINNED_MODELS),
    )
    parser.add_argument("--track", choices=("s", "r"), default="s",
                        help="s = security ablation (default); r = reliability sweep")
    parser.add_argument("--smoke", action="store_true", help="run the fake-LLM smoke test")
    parser.add_argument("--run", action="store_true", help="run the real experiment (author)")
    parser.add_argument("--models", default="", help="comma-separated pinned model keys")
    parser.add_argument("--reps", type=int, default=MIN_REPS, help=f"repetitions/cell (>= {MIN_REPS})")
    parser.add_argument("--ns", default=",".join(str(n) for n in TOOL_COUNTS),
                        help="Track-R tool-count sweep (comma-separated)")
    parser.add_argument("--out", default="", help="directory for raw logs")
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else None
    ns = tuple(int(x) for x in args.ns.split(",") if x.strip())

    if not args.smoke and not args.run:
        print(__doc__ or "")
        parser.print_help()
        return 0

    if args.smoke:
        models: tuple[ModelSpec, ...] = (model_by_key("fake"),)
        reps = args.reps if args.run else min(args.reps, 2)
    else:
        keys = [k.strip() for k in args.models.split(",") if k.strip()]
        if not keys:
            parser.error("--run needs --models (e.g. --models small)")
        models = tuple(model_by_key(k) for k in keys)
        reps = args.reps

    if args.track == "r":
        return _reliability(models, ns, reps, out_dir, smoke=args.smoke)
    return _security(models, reps, out_dir, smoke=args.smoke)


if __name__ == "__main__":
    sys.exit(main())
