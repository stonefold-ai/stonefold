"""M0 — enforce() default-deny stub + audit (RFC §6.2 rule 1, §11, §12 step 1).

M0 DoD: enforce() denies-by-default with an audit stub; unknown ⇒ DENY.
"""

from __future__ import annotations

from stonefold_core import (
    Actor,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
)
from tests.conftest import min_registry


def _ctx() -> tuple[Actor, Session, InMemoryAuditSink]:
    return Actor(id="alice"), Session(id="s1", correlation_id="corr-1"), InMemoryAuditSink()


def test_known_action_defaults_to_deny() -> None:
    reg = min_registry()
    actor, session, audit = _ctx()
    result = enforce(
        RawCall(resource="Customer", action="read"),
        actor,
        session,
        registry=reg,
        audit=audit,
        agent="support-assistant",
    )
    assert result.decision is Decision.DENY
    assert result.rule == "default-deny"


def test_unknown_action_denies_with_unknown_rule() -> None:
    reg = min_registry()
    actor, session, audit = _ctx()
    result = enforce(
        RawCall(resource="Ghost", action="read"),
        actor,
        session,
        registry=reg,
        audit=audit,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "unknown-action"


def test_every_outcome_writes_one_audit_record() -> None:
    reg = min_registry()
    actor, session, audit = _ctx()
    enforce(
        RawCall(resource="Customer", action="read"),
        actor,
        session,
        registry=reg,
        audit=audit,
        agent="support-assistant",
    )
    assert len(audit.records) == 1
    rec = audit.records[0]
    assert rec.agent == "support-assistant"
    assert rec.actor == "alice"
    assert rec.kind == "observe"
    assert rec.resource == "Customer"
    assert rec.action == "read"
    assert rec.decision is Decision.DENY
    assert rec.outcome == "not_executed"
    assert rec.correlationId == "corr-1"
    assert rec.id.startswith("aud_")


def test_unknown_action_audit_has_null_kind() -> None:
    reg = min_registry()
    actor, session, audit = _ctx()
    enforce(
        RawCall(resource="Ghost", action="boo"),
        actor,
        session,
        registry=reg,
        audit=audit,
    )
    rec = audit.records[0]
    assert rec.kind is None
    assert rec.resource == "Ghost"  # falls back to the raw call's resource
    assert rec.decision is Decision.DENY


def test_audit_replay_by_correlation() -> None:
    reg = min_registry()
    actor, session, audit = _ctx()
    for _ in range(3):
        enforce(
            RawCall(resource="Customer", action="read"),
            actor,
            session,
            registry=reg,
            audit=audit,
        )
    assert len(audit.by_correlation("corr-1")) == 3
