# SPDX-License-Identifier: Apache-2.0
"""Render the matrix as Markdown (docs/15 §3). The report is *simultaneously* the
benchmark result, the positioning artifact, and the honest disclosure of where SIF
is overkill — so it prints the A1 row, the S2≈S3 ties, and the token cost in one
place (§3/§6). It never asserts a number is a result: a SMOKE header says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from stonefold_bench.matrix import Matrix


@dataclass(frozen=True)
class ReportMeta:
    smoke: bool
    models: tuple[str, ...]
    reps: int
    unconfigured_rungs: tuple[str, ...] = ()
    unwired_attacks: tuple[str, ...] = ()


_SMOKE_BANNER = (
    "> **SMOKE TEST — NOT A RESULT.** Produced by the deterministic fake LLM to prove\n"
    "> the harness runs end to end. These figures are meaningless as measurements.\n"
    "> Real execution, baseline-fairness sign-off, and publication are the author's,\n"
    "> personally (docs/15 §6–§7). No number here may be quoted anywhere.\n"
)

_REAL_BANNER = (
    "> Benchmark matrix (docs/15). Publish only alongside the harness, gateway\n"
    "> configs, registries/policies, and raw logs (§5–§6). Report the A1 row and any\n"
    "> S2≈S3 ties honestly — the claim is divergence on A3–A7, not everywhere (§6).\n"
)


def _pct(x: float) -> str:
    return f"{100.0 * x:4.0f}%"


def render(matrix: Matrix, meta: ReportMeta) -> str:
    rungs = matrix.rungs()
    lines: list[str] = []
    lines.append("# Benchmark matrix — attack class × defense rung\n")
    lines.append(_SMOKE_BANNER if meta.smoke else _REAL_BANNER)
    lines.append("")
    lines.append(f"Models: {', '.join(meta.models) or '—'} · repetitions/cell: {meta.reps}")
    lines.append("")

    # ASR matrix: each cell = executed / attempted (§3).
    header = "| attack | " + " | ".join(rungs) + " |"
    sep = "|" + "---|" * (len(rungs) + 1)
    lines.append("### ASR — executed / attempted (success = *executed*, §3)")
    lines.append(header)
    lines.append(sep)
    for scenario in matrix.scenarios():
        row = [scenario]
        for rung in rungs:
            c = matrix.cell(scenario, rung)
            row.append(f"{_pct(c.asr_executed)} / {_pct(c.asr_attempted)}" if c else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # BTS row (utility next to security, §3).
    lines.append("### BTS — benign task success (per rung)")
    lines.append("| metric | " + " | ".join(rungs) + " |")
    lines.append(sep)
    bts_row = ["BTS"]
    for rung in rungs:
        bts_row.append(_pct(matrix.bts[rung]) if rung in matrix.bts else "—")
    lines.append("| " + " | ".join(bts_row) + " |")
    lines.append("")

    # Token cost per rung (§4.2): mean over that rung's attack cells.
    lines.append("### Token cost — mean tokens/run (per rung)")
    lines.append("| metric | " + " | ".join(rungs) + " |")
    lines.append(sep)
    tok_row = ["tokens"]
    for rung in rungs:
        cells = [c for c in matrix.cells if c.rung == rung]
        mean = sum(c.tokens_mean for c in cells) / len(cells) if cells else None
        tok_row.append(f"{mean:.0f}" if mean is not None else "—")
    lines.append("| " + " | ".join(tok_row) + " |")
    lines.append("")

    if meta.unconfigured_rungs:
        lines.append(
            f"**UNCONFIGURED rungs** (author supplies the policy, §4.4): "
            f"{', '.join(meta.unconfigured_rungs)}"
        )
    if meta.unwired_attacks:
        lines.append(
            f"**UNWIRED attack slots** (author sources the scenario, §7): "
            f"{', '.join(meta.unwired_attacks)}"
        )
    lines.append("")
    return "\n".join(lines)
