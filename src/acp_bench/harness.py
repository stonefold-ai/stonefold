"""Track-S orchestrator: drive the security matrix over models × rungs × attacks
(docs/15 §3/§5). Pure orchestration — file I/O belongs to the caller (``__main__``),
which subscribes via the callbacks.

**Repetition is the outermost loop** (mirroring ``reliability.run_reliability``): an
interrupted run leaves a *complete* matrix at fewer repetitions — every configured
rung covered — rather than many reps of only the first rung. ``on_trial`` fires as
each trial finishes (the CLI appends it to the raw log immediately); ``on_round``
fires after each full sweep (the CLI rewrites the aggregated cells files there, so a
graph can be drawn while the run is still going).

Track-R orchestration lives in ``reliability`` with the same callback contract.
"""

from __future__ import annotations

from collections.abc import Callable

from acp_bench.attacks import Attack
from acp_bench.conditions import Condition, is_configured
from acp_bench.matrix import Matrix, aggregate
from acp_bench.model import ModelSpec
from acp_bench.runner import Trial, run_attack_trial, run_benign_trial


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
    on_trial: Callable[[Trial], None] | None = None,
    on_round: Callable[[int, list[Trial]], None] | None = None,
) -> tuple[Matrix, list[Trial]]:
    """Run every (model × configured-rung × [benign + wired-attack]) cell for ``reps``
    repetitions, rep-outermost. UNCONFIGURED rungs and UNWIRED attacks are skipped
    here and surfaced to the caller via ``unconfigured_rungs`` / ``unwired_attacks``
    (never faked). ``on_trial(trial)`` is called as each trial completes;
    ``on_round(rep, trials_so_far)`` after each full repetition sweep."""
    trials: list[Trial] = []

    def _record(trial: Trial) -> None:
        trials.append(trial)
        if on_trial is not None:
            on_trial(trial)

    for rep in range(reps):
        for spec in models:
            for cond in conditions:
                if not is_configured(cond):
                    continue
                _record(run_benign_trial(spec, cond, rep))
                for attack in attacks:
                    if attack.wired:
                        _record(run_attack_trial(spec, cond, attack, rep))
        if on_round is not None:
            on_round(rep, list(trials))
    return aggregate(trials), trials
