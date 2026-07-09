"""v0.6 agent feedback visibility — CS-030 (RFC §11).

What the agent receives on a deny/hold is a declared choice: ``code`` |
``code+fields`` (the new default) | ``code+evidence``. Redaction happens at the
transport on the return path; the audit record is written from the full result
first — redact on return, never on write. Plus the probing-detection surface:
deny-rate and reason-code distribution per principal.
"""

from __future__ import annotations

from typing import Any

from stonefold_core import (
    Actor,
    Decision,
    FeedbackLevel,
    InMemoryAuditSink,
    RetryClass,
    Session,
)
from stonefold_core.feedback import agent_view
from stonefold_gates.base import check_hold
from stonefold_gates.engine import DefaultGateEngine
from stonefold_gateway.transport import Gateway
from stonefold_gateway.admin_api import reason_code_stats
from stonefold_store import InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

from stonefold_core import load_policy

HOLD_CHECK = "matchesOpenPurchaseOrder"


def _gateway(
    gates: dict[str, Any], *, preconditions: dict[str, Any] | None = None
) -> tuple[Gateway, InMemoryAuditSink]:
    reg = full_registry()
    doc = {
        "agent": "t",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": gates},
    }
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    gw = Gateway(
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg, preconditions=preconditions or {}),
        outbox=InMemoryOutboxStore(audit=audit),
    )
    return gw, audit


def _submit(gw: Gateway, data: dict[str, Any]) -> Any:
    return gw.submit(
        resource="Payment", action="pay", data=data,
        actor=Actor(id="alice"), session=Session(id="s1"),
    )


VALUE_GATE = {"valueLimit": {"field": "data.amount", "max": 100}}


def test_default_code_fields_strips_prose_keeps_code_and_fields() -> None:
    gw, audit = _gateway(VALUE_GATE)
    result = _submit(gw, {"amount": 500})
    assert result.decision is Decision.DENY
    assert result.feedback is FeedbackLevel.CODE_FIELDS
    assert result.reason_code == "gate:valueLimit"
    assert result.retry_class is RetryClass.RETRYABLE
    deciding = [g for g in result.gates if g.gate == "valueLimit"][0]
    assert deciding.reason == ""  # prose stripped: it carries the policy constant
    assert deciding.fields == ("data.amount",)  # ...but WHICH field failed survives

    # redact on return, never on write: the audit kept the full prose.
    audited = [g for g in audit.records[-1].gates if g.gate == "valueLimit"][0]
    assert "exceeds max" in audited.reason


def test_code_level_strips_the_trace_entirely() -> None:
    gw, audit = _gateway({**VALUE_GATE, "feedback": "code"})
    result = _submit(gw, {"amount": 500})
    assert result.decision is Decision.DENY
    assert result.gates == ()
    assert result.scope_applied == ()
    # the loop's signal always passes through
    assert result.reason_code == "gate:valueLimit"
    assert result.retry_class is RetryClass.RETRYABLE
    assert audit.records[-1].gates  # audit unaffected


def test_code_evidence_is_the_full_view() -> None:
    gw, _ = _gateway({**VALUE_GATE, "feedback": "code+evidence"})
    result = _submit(gw, {"amount": 500})
    deciding = [g for g in result.gates if g.gate == "valueLimit"][0]
    assert "exceeds max" in deciding.reason


def test_hold_evidence_is_stripped_at_default_kept_at_evidence_level() -> None:
    hold = {
        "precondition": {"checks": [HOLD_CHECK], "resolvers": "role:ap-clerk"},
    }
    checks = {HOLD_CHECK: lambda gctx: check_hold(
        "multiple-candidates", {"candidates": ["PO-1", "PO-2"]}
    )}

    gw, audit = _gateway(hold, preconditions=checks)
    result = _submit(gw, {"amount": 50})
    assert result.decision is Decision.HOLD
    held_trace = [g for g in result.gates if g.gate == "precondition"][0]
    assert held_trace.evidence is None  # record-side data never reaches the agent
    assert held_trace.code == "multiple-candidates"
    # the audit (and hence the resolver's queue) kept the evidence
    audited = [g for g in audit.records[-1].gates if g.gate == "precondition"][0]
    assert audited.evidence == {"candidates": ["PO-1", "PO-2"]}

    gw2, _ = _gateway({**hold, "feedback": "code+evidence"}, preconditions=checks)
    result2 = _submit(gw2, {"amount": 50})
    trace2 = [g for g in result2.gates if g.gate == "precondition"][0]
    assert trace2.evidence == {"candidates": ["PO-1", "PO-2"]}


def test_allow_results_pass_output_through() -> None:
    gw, _ = _gateway(VALUE_GATE)
    result = _submit(gw, {"amount": 50})
    assert result.decision is Decision.ALLOW
    assert result.ticket is not None  # staged effect receipt untouched


def test_agent_view_is_a_pure_projection() -> None:
    # Determinism spot-check (invariant 1): same input, same output, no mutation.
    gw, _ = _gateway({**VALUE_GATE, "feedback": "code+evidence"})
    full = _submit(gw, {"amount": 500})
    once = agent_view(full, FeedbackLevel.CODE)
    twice = agent_view(full, FeedbackLevel.CODE)
    assert once == twice
    assert full.gates  # the source was not mutated


def test_reason_code_stats_distribution() -> None:
    gw, audit = _gateway(VALUE_GATE)
    for amount in (500, 600, 700, 50):
        _submit(gw, {"amount": amount})
    (entry,) = reason_code_stats(audit.all_records())
    assert entry["agent"] == "t"
    assert entry["actor"] == "alice"
    assert entry["total"] == 4
    assert entry["denied"] == 3
    assert entry["denyRate"] == 0.75
    assert entry["codes"] == {"gate:valueLimit": 3}
