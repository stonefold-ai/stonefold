"""The orchestrator: run the Track-S security matrix over a set of models, conditions,
and attacks, writing the raw logs and returning the aggregated matrix (docs/15 §3/§5).

Reliability (Track R) surface construction + scoring live in ``tracks``; wiring a full
Track-R run needs a task set and real models (the author's, §7), so it is not driven
here beyond what the smoke exercises.
"""

from __future__ import annotations

from pathlib import Path

from acp_bench.attacks import Attack
from acp_bench.conditions import Condition, is_configured
from acp_bench.matrix import Matrix, aggregate
from acp_bench.model import ModelSpec
from acp_bench.raw_log import write_jsonl
from acp_bench.runner import Trial, run_attack_cell, run_benign_cell


def unconfigured_rungs(conditions: tuple[Condition, ...]) -> tuple[str, ...]:
    return tuple(c.rung.value for c in conditions if not is_configured(c))


def unwired_attacks(attacks: tuple[Attack, ...]) -> tuple[str, ...]:
    return tuple(a.cls for a in attacks if not a.wired)


def run_security(
    models: tuple[ModelSpec, ...],
    conditions: tuple[Condition, ...],
    attacks: tuple[Attack, ...],
    *,
    reps: int,
    out_dir: Path | None = None,
) -> tuple[Matrix, list[Trial]]:
    """Run every (model × configured-rung × [benign + wired-attack]) cell for ``reps``
    repetitions. UNCONFIGURED rungs and UNWIRED attacks are skipped here and surfaced
    to the caller via ``unconfigured_rungs`` / ``unwired_attacks`` (never faked)."""
    trials: list[Trial] = []
    for spec in models:
        for cond in conditions:
            if not is_configured(cond):
                continue
            trials.extend(run_benign_cell(spec, cond, reps=reps))
            for attack in attacks:
                trials.extend(run_attack_cell(spec, cond, attack, reps=reps))
    if out_dir is not None:
        write_jsonl(out_dir / "security_trials.jsonl", trials)
    return aggregate(trials), trials
