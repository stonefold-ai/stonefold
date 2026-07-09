"""Audit sink protocol and the in-memory implementation (RFC §11, design §11).

Every evaluated action — allowed, held, denied, or halted — produces exactly one
append-only record. The pipeline calls ``write`` from its terminal/hold paths.
This module defines the seam; durable Postgres/WORM sinks plug in later (M6).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from stonefold_core.enums import Decision
from stonefold_core.models import (
    Actor,
    AuditRecord,
    EvalResult,
    GateResult,
    RawCall,
    ResolvedAction,
    Session,
)


def obligation_refs(gates: list[GateResult] | tuple[GateResult, ...]) -> dict[str, Any] | None:
    """The CS-037 ``obligationRefs`` audit field, lifted from the
    ``requireMatch`` gate's trace evidence (registry, matched/candidate refs,
    candidate count). ``None`` when no ``requireMatch`` gate ran — the field is
    entitlement lineage, not a default."""
    for g in gates:
        if g.gate == "requireMatch" and g.evidence and "registry" in g.evidence:
            return {
                "registry": g.evidence.get("registry"),
                "refs": list(g.evidence.get("refs") or []),
                "candidates": g.evidence.get("candidates"),
            }
    return None


class AuditSink(Protocol):
    """Append-only audit sink (design §11). Implementations MUST NOT mutate or
    delete prior records."""

    def write(self, record: AuditRecord) -> None: ...


class InMemoryAuditSink:
    """A list-backed sink for tests and the earliest milestones."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)

    def by_correlation(self, correlation_id: str) -> list[AuditRecord]:
        """Replay one agent run as an ordered query (RFC §11)."""
        return [r for r in self.records if r.correlationId == correlation_id]

    def all_records(self) -> list[AuditRecord]:
        """Every record, in write order (the CS-030 stats surface reads this)."""
        return list(self.records)


class FallbackAuditSink:
    """Best-effort durability for the audit write (design §11 review note, F3).

    Writes to ``primary`` (the durable DB sink); if that raises — the audit DB is
    down — it falls back to ``fallback`` (an in-memory/file sink) so a fail-closed
    decision is never *also* an unaudited one. This fallback is the *only*
    sanctioned side channel: the design forbids best-effort logging in general
    ("the audit is the product's evidence"), but losing the record of a refusal is
    worse than writing it somewhere recoverable. ``failures`` counts primary write
    failures for observability/alerting.
    """

    def __init__(self, *, primary: AuditSink, fallback: AuditSink) -> None:
        self._primary = primary
        self._fallback = fallback
        self.failures = 0

    def write(self, record: AuditRecord) -> None:
        try:
            self._primary.write(record)
        except Exception:  # the durable sink is down ⇒ keep the record anyway
            self.failures += 1
            self._fallback.write(record)


def build_record(
    *,
    agent: str,
    actor: Actor,
    session: Session,
    call: RawCall,
    resolved: ResolvedAction | None,
    result: EvalResult,
    outcome: str = "not_executed",
    approval: dict[str, Any] | None = None,
    result_refs: list[str] | None = None,
    consumption: dict[str, Any] | None = None,
) -> AuditRecord:
    """Assemble an ``AuditRecord`` from a terminal evaluation.

    ``id``/``timestamp`` are generated here (the only non-determinism in the
    audit layer — it sits *outside* ``enforce``'s deterministic decision logic,
    which is what invariant 1 protects).
    """

    return AuditRecord(
        id=f"aud_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc),
        agent=agent,
        actor=actor.id,
        kind=resolved.kind.value if resolved is not None else None,
        resource=resolved.resource if resolved is not None else call.resource,
        action=resolved.action if resolved is not None else call.action,
        parameters=dict(call.data),
        scopeApplied=list(result.scope_applied),
        gates=list(result.gates),
        decision=result.decision,
        rule=result.rule,
        reasonCode=result.reason_code,
        retryClass=result.retry_class,
        approval=approval,
        outcome=outcome,
        resultRefs=list(result_refs or []),
        obligationRefs=obligation_refs(result.gates),
        consumption=consumption,
        correlationId=session.correlation_id or session.id,
    )
