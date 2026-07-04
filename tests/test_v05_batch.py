"""v0.5 CS-023 — batch decision semantics (RFC §12; SIF §5).

Acceptance H1–H4: a SIF batch is decided atomically — every operation is
decided first (each with its own audit record); any DENY/HALT refuses the whole
batch before anything commits or stages; a HOLD stages ``PENDING_APPROVAL`` and
does not refuse the batch; a later rejection does not roll committed ops back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stonefold_core import (
    Actor,
    BatchResult,
    Connectors,
    Decision,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    Session,
    enforce_batch,
    load_policy,
)
from stonefold_core.kill import KillScope, KillScopeKind
from stonefold_core.pipeline import BATCH_REFUSED
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_store import DispatchWorker, InMemoryOutboxStore
from stonefold_store.kill_memory import InMemoryKillStore
from tests.conftest import full_registry, load_schema


def _policy_doc() -> dict[str, Any]:
    return {
        "agent": "batcher",
        "allow": [{"record": ["Note"]}, {"observe": ["Note"]}, {"effect": ["pay"]}],
        "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}},
    }


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    world: InMemoryConnector
    connectors: Connectors
    kill: InMemoryKillStore

    def submit(self, *calls: RawCall) -> BatchResult:
        return enforce_batch(
            list(calls),
            Actor(id="alice"),
            Session(id="s1", correlation_id="corr-b1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            gates=DefaultGateEngine(self.reg),
            connectors=self.connectors,
            outbox=self.outbox,
            kill=self.kill,
        )

    def drain(self) -> int:
        worker = DispatchWorker(
            self.outbox, self.connectors, registry=self.reg, kill=self.kill
        )
        return worker.drain()

    def notes(self) -> list[dict[str, Any]]:
        return self.world.tables.get("Note", [])


def harness() -> Harness:
    reg = full_registry()
    policy = load_policy(_policy_doc(), reg, schema=load_schema())
    audit = InMemoryAuditSink()
    world = InMemoryConnector()
    connectors = Connectors(
        {name: world for name in ("in_memory", "sql", "http", "email")}
    )
    return Harness(
        reg, policy, audit, InMemoryOutboxStore(audit=audit), world, connectors,
        InMemoryKillStore(),
    )


NOTE = RawCall(resource="Note", action="create", data={"text": "invoice logged"})
PAY = RawCall(resource="Payment", action="pay", data={"amount": 800})
EXPORT = RawCall(resource="Export", action="exportData", data={})  # not allowed


# --- H1: any DENY refuses the whole batch ----------------------------------
def test_h1_deny_refuses_batch_before_anything_commits_or_stages() -> None:
    h = harness()
    result = h.submit(NOTE, EXPORT)

    assert result.decision is Decision.DENY
    assert result.failing_index == 1
    # nothing committed, nothing staged (CS-023: "before anything commits or stages")
    assert h.notes() == []
    assert h.outbox.list_by_state(PendingState.PENDING) == []
    assert h.outbox.list_by_state(PendingState.PENDING_APPROVAL) == []
    assert h.world.effects == []

    # every operation carries its own audit record (RFC §11)
    assert len(h.audit.records) == 2
    first, second = h.audit.records
    assert first.decision is Decision.ALLOW and first.outcome == BATCH_REFUSED
    assert second.decision is Decision.DENY and second.rule == "default-deny"

    # the per-operation results mirror the records
    assert [r.decision for r in result.results] == [Decision.ALLOW, Decision.DENY]


# --- H2: a HALT refuses the whole batch the same way ------------------------
def test_h2_halt_refuses_batch() -> None:
    h = harness()
    h.kill.issue(
        KillScope(kind=KillScopeKind.ACTION_CLASS, resource="Note", action="create"),
        issued_by="operator",
    )
    result = h.submit(NOTE, PAY)

    assert result.decision is Decision.HALT
    assert result.failing_index == 0
    assert h.notes() == []
    # the other op was decided HOLD but the refused batch stages NOTHING
    assert result.results[1].decision is Decision.HOLD
    assert result.results[1].ticket is None
    assert h.outbox.list_by_state(PendingState.PENDING_APPROVAL) == []
    halted = h.audit.records[0]
    assert halted.decision is Decision.HALT and halted.outcome == "halted"
    assert h.audit.records[1].outcome == BATCH_REFUSED


# --- H3: a HOLD does not refuse the batch -----------------------------------
def test_h3_hold_commits_batch_and_stages_pending_approval() -> None:
    h = harness()
    result = h.submit(NOTE, PAY)

    assert result.decision is Decision.HOLD
    assert result.failing_index is None
    # the record op committed atomically with the staging (§4.4)
    assert len(h.notes()) == 1
    held = result.results[1]
    assert held.decision is Decision.HOLD and held.ticket is not None
    assert h.outbox.get(held.ticket).state is PendingState.PENDING_APPROVAL

    # nothing dispatches until the approval releases it
    h.drain()
    assert h.world.effects == []
    h.outbox.approve(held.ticket, "carol")
    h.drain()
    assert [e["action"] for e in h.world.effects] == ["pay"]


# --- H4: a later rejection does not roll committed ops back ------------------
def test_h4_rejection_does_not_roll_back_committed_record_ops() -> None:
    h = harness()
    result = h.submit(NOTE, PAY)
    held = result.results[1]
    assert held.ticket is not None

    h.outbox.reject(held.ticket, "carol")
    h.drain()
    assert h.world.effects == []  # the held effect never dispatches
    assert len(h.notes()) == 1  # ...but the committed record op remains (CS-023)


# --- the whole-batch record consistency: per-op audit on commit too ----------
def test_committed_batch_audits_every_operation() -> None:
    h = harness()
    h.submit(NOTE, PAY)
    assert [r.decision for r in h.audit.records] == [Decision.ALLOW, Decision.HOLD]
    assert h.audit.records[0].outcome == "success"
    assert all(r.correlationId == "corr-b1" for r in h.audit.records)


# --- the SIF wire form over HTTP (SIF §5/§6) ---------------------------------
def _wire_client() -> tuple[Any, InMemoryAuditSink]:
    from fastapi.testclient import TestClient

    from stonefold_gateway.main import create_app
    from stonefold_gateway.transport import Gateway

    h = harness()
    gw = Gateway(
        registry=h.reg, audit=h.audit, policy=h.policy,
        gates=DefaultGateEngine(h.reg), outbox=h.outbox, connectors=h.connectors,
    )
    return TestClient(create_app(gw)), h.audit


def test_wire_batch_refusal_names_the_failing_operation() -> None:
    client, _ = _wire_client()
    r = client.post(
        "/submit_intent",
        json={"operations": [
            {"resource": "Note", "action": "create", "data": {"text": "t"}},
            {"resource": "Export", "action": "exportData"},
        ]},
        headers={"X-Actor-Id": "alice", "X-Session-Id": "s1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny"
    assert body["error"]["pointer"] == "operations[1]"  # SIF §6 structured error
    assert len(body["operations"]) == 2


def test_wire_single_operation_shape_is_unchanged() -> None:
    client, _ = _wire_client()
    r = client.post(
        "/submit_intent",
        json={"resource": "Note", "action": "create", "data": {"text": "t"}},
        headers={"X-Actor-Id": "alice", "X-Session-Id": "s1"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"  # flat pre-batch response, no wrapper
    assert "operations" not in r.json()
