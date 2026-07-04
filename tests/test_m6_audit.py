"""M6 — audit completeness and replay (RFC §11, design §11).

Acceptance **F1**: for each of ALLOW / HOLD / DENY / HALT a corresponding
append-only record exists, carrying the RFC §11 "required at full" fields. Plus
the replay query: one agent run replays as one ordered query by ``correlationId``
(design §11). The durable Postgres sink is round-tripped in the integration test.
"""

from __future__ import annotations

from typing import Any

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    KillScope,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_connectors import InMemoryConnector
from stonefold_core.models import AuditRecord
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry, load_schema

# The RFC §11 "required at full" columns. ``approval`` is excluded: the RFC marks
# it "if applicable", so ``None`` is a legal value (only HOLD/approval rows carry it).
_REQUIRED_AT_FULL = (
    "id",
    "timestamp",
    "agent",
    "actor",
    "kind",
    "resource",
    "action",
    "parameters",
    "scopeApplied",
    "gates",
    "decision",
    "outcome",
    "correlationId",
)


def _assert_required_fields(record: AuditRecord) -> None:
    for field in _REQUIRED_AT_FULL:
        assert getattr(record, field) is not None, f"{field} missing on {record.decision}"


def _enforce(
    audit: InMemoryAuditSink,
    doc: dict[str, Any],
    *,
    resource: str,
    action: str,
    kill: Any = None,
    data: dict[str, Any] | None = None,
    correlation: str = "corr-F1",
    session: str = "s1",
) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    outbox = InMemoryOutboxStore(audit=audit)
    connectors = Connectors(
        {"in_memory": InMemoryConnector(), "email": InMemoryConnector(), "sql": InMemoryConnector()}
    )
    return enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        Actor(id="alice"),
        Session(id=session, correlation_id=correlation),
        registry=reg,
        audit=audit,
        policy=policy,
        gates=DefaultGateEngine(reg),
        outbox=outbox,
        connectors=connectors,
        kill=kill,
    )


# --- F1: every outcome is recorded with the required fields ---------------
def test_f1_allow_is_recorded() -> None:
    audit = InMemoryAuditSink()
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}
    result = _enforce(audit, doc, resource="Email", action="sendEmail",
                      data={"to": "x@acme.example"})
    assert result.decision is Decision.ALLOW
    allows = [r for r in audit.records if r.decision is Decision.ALLOW]
    assert len(allows) == 1
    _assert_required_fields(allows[0])
    assert allows[0].outcome == "staged"  # effect staged, not yet sent


def test_f1_hold_is_recorded() -> None:
    audit = InMemoryAuditSink()
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}},
    }
    result = _enforce(audit, doc, resource="Payment", action="pay", data={"amount": 1})
    assert result.decision is Decision.HOLD
    holds = [r for r in audit.records if r.decision is Decision.HOLD]
    assert len(holds) == 1
    _assert_required_fields(holds[0])
    assert holds[0].approval is not None  # the approval contract is recorded on a HOLD


def test_f1_deny_is_recorded() -> None:
    audit = InMemoryAuditSink()
    # refund is not in the allow set ⇒ default-deny.
    doc = {"agent": "pay", "allow": [{"effect": ["pay"]}]}
    result = _enforce(audit, doc, resource="Payment", action="refund", data={"amount": 1})
    assert result.decision is Decision.DENY
    denies = [r for r in audit.records if r.decision is Decision.DENY]
    assert len(denies) == 1
    _assert_required_fields(denies[0])
    assert denies[0].outcome == "not_executed"


def test_f1_halt_is_recorded() -> None:
    audit = InMemoryAuditSink()
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("s1"), issued_by="operator")
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}
    result = _enforce(audit, doc, resource="Email", action="sendEmail",
                      data={"to": "x@acme.example"}, kill=kill)
    assert result.decision is Decision.HALT
    halts = [r for r in audit.records if r.decision is Decision.HALT]
    assert len(halts) == 1
    _assert_required_fields(halts[0])
    assert halts[0].outcome == "halted"


def test_f1_all_four_decisions_in_one_sink() -> None:
    # The same append-only sink captures one of each terminal decision.
    audit = InMemoryAuditSink()
    kill = InMemoryKillStore()
    kill.issue(KillScope.for_session("killed"), issued_by="operator")

    _enforce(audit, {"agent": "support", "allow": [{"effect": ["sendEmail"]}]},
             resource="Email", action="sendEmail", data={"to": "x@acme.example"})
    _enforce(audit, {"agent": "pay", "allow": [{"effect": ["pay"]}],
                     "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}}},
             resource="Payment", action="pay", data={"amount": 1})
    _enforce(audit, {"agent": "pay", "allow": [{"effect": ["pay"]}]},
             resource="Payment", action="refund", data={"amount": 1})
    _enforce(audit, {"agent": "support", "allow": [{"effect": ["sendEmail"]}]},
             resource="Email", action="sendEmail", data={"to": "x@acme.example"},
             kill=kill, session="killed")

    seen = {r.decision for r in audit.records}
    assert {Decision.ALLOW, Decision.HOLD, Decision.DENY, Decision.HALT} <= seen
    for record in audit.records:
        _assert_required_fields(record)


# --- replay query (design §11): one run, one ordered query ----------------
def test_replay_by_correlation_is_ordered() -> None:
    audit = InMemoryAuditSink()
    doc = {"agent": "support", "allow": [{"observe": ["read"]}, {"effect": ["sendEmail"]}]}

    _enforce(audit, doc, resource="Customer", action="read", correlation="run-A")
    _enforce(audit, doc, resource="Email", action="sendEmail",
             data={"to": "x@acme.example"}, correlation="run-A")
    # a second run shares the sink but a different correlation id
    _enforce(audit, doc, resource="Customer", action="read", correlation="run-B")

    run_a = audit.by_correlation("run-A")
    assert [r.resource for r in run_a] == ["Customer", "Email"]  # insertion order
    assert all(r.correlationId == "run-A" for r in run_a)
    assert len(audit.by_correlation("run-B")) == 1
