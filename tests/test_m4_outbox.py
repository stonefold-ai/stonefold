"""M4 — effects, outbox, approvals (RFC §4.4, design §7/§9). Acceptance D1–D4.

Driven through the in-memory outbox + dispatch worker; the Postgres
``SELECT … FOR UPDATE`` path is exercised in ``test_m4_pg_integration.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from stonefold_core import (
    Actor,
    ConnectorResult,
    Connectors,
    Decision,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    ResolvedAction,
    ScopePredicate,
    SelfApprovalError,
    Session,
    enforce,
    load_policy,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import full_registry, load_schema


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    engine: DefaultGateEngine
    connectors: Connectors
    worker: DispatchWorker
    effect_conn: Any

    def enforce(self, resource: str, action: str, actor: Actor, data: dict[str, Any] | None = None) -> Any:
        return enforce(
            RawCall(resource=resource, action=action, data=data or {}),
            actor,
            Session(id="s1", correlation_id="corr-1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=self.engine,
            outbox=self.outbox,
        )

    def get(self, ticket: str) -> Any:
        row = self.outbox.get(ticket)
        assert row is not None
        return row


def harness(doc: dict[str, Any], *, connector: Any | None = None) -> Harness:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    effect_conn = connector or InMemoryConnector()
    connectors = Connectors({"in_memory": effect_conn, "email": effect_conn, "sql": effect_conn})
    worker = DispatchWorker(outbox, connectors, registry=reg)
    return Harness(reg, policy, audit, outbox, DefaultGateEngine(reg), connectors, worker, effect_conn)


# --- D1: staged then dispatched exactly once -----------------------------
def test_d1_effect_staged_then_dispatched_exactly_once() -> None:
    h = harness({"agent": "support", "allow": [{"effect": ["sendEmail"]}]})
    result = h.enforce("Email", "sendEmail", Actor(id="alice"), {"to": "x@acme.example"})
    assert result.decision is Decision.ALLOW
    assert result.ticket is not None
    assert h.get(result.ticket).state is PendingState.PENDING
    assert h.effect_conn.effects == []  # nothing dispatched on the request turn

    assert h.worker.drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE
    assert len(h.effect_conn.effects) == 1

    # a forced worker retry does NOT double-send (row is DONE; idempotent anyway)
    assert h.worker.drain() == 0
    assert len(h.effect_conn.effects) == 1


def test_d1_dispatch_is_idempotent_on_key() -> None:
    conn = InMemoryConnector()
    action = full_registry().resolve(RawCall(resource="Email", action="sendEmail", data={"to": "x"}))
    r1 = conn.dispatch(action, Actor(id="alice"), "key-123")
    r2 = conn.dispatch(action, Actor(id="alice"), "key-123")
    assert len(conn.effects) == 1  # the second call did not re-send
    assert r1.handle == r2.handle == "key-123"


# --- D2: approval suspends and releases ----------------------------------
def _approval_doc() -> dict[str, Any]:
    return {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}},
    }


def test_d2_approval_suspends_then_releases() -> None:
    h = harness(_approval_doc())
    result = h.enforce("Payment", "pay", Actor(id="alice"), {"amount": 1})
    assert result.decision is Decision.HOLD
    assert h.get(result.ticket).state is PendingState.PENDING_APPROVAL
    assert h.worker.drain() == 0  # nothing dispatched while held

    h.outbox.approve(result.ticket, "boss")
    assert h.get(result.ticket).state is PendingState.PENDING
    assert h.worker.drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


def test_d2_reject_never_dispatches() -> None:
    h = harness(_approval_doc())
    result = h.enforce("Payment", "pay", Actor(id="alice"), {"amount": 1})
    h.outbox.reject(result.ticket, "boss")
    assert h.get(result.ticket).state is PendingState.CANCELLED
    assert h.worker.drain() == 0
    assert h.effect_conn.effects == []


# --- D3: dual authorization rejects self-approval ------------------------
def test_d3_dual_authorization() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "gates": {"pay": {"dualAuthorization": {"approvers": "role:treasury"}}},
    }
    h = harness(doc)
    result = h.enforce("Payment", "pay", Actor(id="alice"), {"amount": 1})
    assert result.decision is Decision.HOLD

    # the actor cannot approve its own action
    with pytest.raises(SelfApprovalError):
        h.outbox.approve(result.ticket, "alice")

    # one distinct approver is not enough (quorum 2)
    h.outbox.approve(result.ticket, "bob")
    assert h.get(result.ticket).state is PendingState.PENDING_APPROVAL
    # the same approver again does not advance quorum
    h.outbox.approve(result.ticket, "bob")
    assert h.get(result.ticket).state is PendingState.PENDING_APPROVAL
    # a second distinct approver releases it
    h.outbox.approve(result.ticket, "carol")
    assert h.get(result.ticket).state is PendingState.PENDING
    assert h.worker.drain() == 1


# --- D4: failed irreversible effect stages compensation ------------------
class _FailingConnector:
    def execute(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> ConnectorResult:
        return ConnectorResult(kind="receipt", receipt={})

    def dispatch(self, action: ResolvedAction, actor: Actor, idempotency_key: str) -> ConnectorResult:
        raise RuntimeError("downstream rejected the prescription")

    def fetch_target(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> dict[str, Any] | None:
        return dict(action.data)

    def cancel(self, handle: str) -> None:
        return None


def test_d4_failed_irreversible_stages_compensation() -> None:
    h = harness({"agent": "rx", "allow": [{"effect": ["prescribe"]}]}, connector=_FailingConnector())
    result = h.enforce("Prescribing", "prescribe", Actor(id="alice"), {"drug": "X"})
    assert result.decision is Decision.ALLOW
    staged = h.get(result.ticket)
    assert staged.compensation is not None and staged.compensation.action == "discontinue"

    h.worker.run_once()  # dispatch fails
    assert h.get(result.ticket).state is PendingState.FAILED
    # the declared compensating effect is now staged PENDING and audited
    pending = h.outbox.list_by_state(PendingState.PENDING)
    assert any(p.resolved.action == "discontinue" for p in pending)
    assert any(r.outcome == "failure" for r in h.audit.records)


def test_d4_reversible_failure_stages_no_compensation() -> None:
    h = harness({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, connector=_FailingConnector())
    result = h.enforce("Email", "sendEmail", Actor(id="alice"), {"to": "x@acme.example"})
    h.worker.run_once()
    assert h.get(result.ticket).state is PendingState.FAILED
    assert h.outbox.list_by_state(PendingState.PENDING) == []  # nothing to compensate
