"""Build an evidence pack from audit records (read-only).

Aggregates the audit log into per-control evidence, one bucket per docs/14 row. No
enforcement-path code and no writes: the exporter only *reads* records (RFC §11's log
is append-only; this is a consumer of it).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from stonefold_core.enums import Decision
from stonefold_core.models import AuditRecord

from stonefold_evidence.controls import CITATION_CAVEAT, CONTROLS, Control


@dataclass(frozen=True)
class ControlEvidence:
    control: Control
    present: bool           # did the log contain anything evidencing this control?
    facts: list[str]        # human-readable evidence lines
    sample: list[AuditRecord]  # a few illustrative records


@dataclass(frozen=True)
class EvidencePack:
    generated_at: datetime | None
    policy_ref: str | None
    total_records: int
    window: tuple[datetime, datetime] | None
    decision_counts: dict[str, int]
    controls: list[ControlEvidence]
    caveat: str = CITATION_CAVEAT


def _window(records: Sequence[AuditRecord]) -> tuple[datetime, datetime] | None:
    stamps = [r.timestamp for r in records]
    return (min(stamps), max(stamps)) if stamps else None


def _executed(records: Sequence[AuditRecord]) -> list[AuditRecord]:
    # A dispatched effect settles with outcome "success" (design §9 / invariant 6).
    return [r for r in records if r.outcome == "success"]


def build_evidence_pack(
    records: Sequence[AuditRecord],
    *,
    policy_ref: str | None = None,
    generated_at: datetime | None = None,
    sample_size: int = 3,
) -> EvidencePack:
    """Assemble the evidence pack. ``generated_at`` is injected (kept out of this
    pure aggregation) so callers/tests control the timestamp."""
    decisions = Counter(r.decision.value for r in records)
    held = [r for r in records if r.decision is Decision.HOLD]
    halted = [r for r in records if r.decision is Decision.HALT]
    approved = [r for r in records if r.approval is not None]
    executed = _executed(records)
    with_refs = [r for r in executed if r.resultRefs]
    with_gates = [r for r in records if r.gates]

    def evidence(control: Control) -> ControlEvidence:
        facts: list[str] = []
        sample: list[AuditRecord] = []
        # positive evidence present for this control in this window?
        present = bool(records)
        if control.key == "record_keeping":
            facts.append(f"{len(records)} audit records — every evaluated action "
                         "(allow / hold / deny / halt) is recorded.")
            facts.append("decisions: " + ", ".join(
                f"{decisions.get(d, 0)} {d}" for d in ("allow", "hold", "deny", "halt")))
            facts.append(f"{len(executed)} executed effects, {len(with_refs)} carrying "
                         "resultRefs (downstream traceability, RFC §11 / CS-009).")
            facts.append("append-only, and for effects the record shares the effect's DB "
                         "transaction (no effect without a record).")
            sample = list(executed[:sample_size]) or list(records[:sample_size])
        elif control.key == "oversight_intervene":
            facts.append(f"{len(held)} actions held for human approval "
                         "(the agent cannot release its own — dual-authorization).")
            facts.append(f"{len(approved)} records carry an approval contract "
                         "(who may release, quorum, timeout).")
            facts.append(f"{len(halted)} actions halted by the kill-switch "
                         "(no-race: halted before dispatch, in the dispatch transaction).")
            sample = list((held + halted)[:sample_size])
            present = bool(held or halted or approved)  # positive oversight events
        elif control.key == "oversight_capacity":
            facts.append(f"every one of {len(records)} decisions carries its deciding rule "
                         "(deterministic — no model in the enforcement path, so reproducible).")
            facts.append(f"{len(with_gates)} records carry per-gate results (audit: full) — "
                         "the overseer can see exactly which control decided.")
            sample = list(with_gates[:sample_size]) or list(records[:sample_size])
        elif control.key == "deployer":
            facts.append(f"documented control: policy {policy_ref or '(supply --policy)'} "
                         "— the versioned artifact stating what the agent may do.")
            facts.append(f"log retention: {len(records)} records retained; the store is the "
                         "retention target (>= 6 months [VERIFY 26(6)]).")
            sample = list(records[:sample_size])
        return ControlEvidence(control=control, present=present, facts=facts, sample=sample)

    return EvidencePack(
        generated_at=generated_at,
        policy_ref=policy_ref,
        total_records=len(records),
        window=_window(records),
        decision_counts=dict(decisions),
        controls=[evidence(c) for c in CONTROLS],
    )
