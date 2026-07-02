"""v0.4 CS-018 — scope no-race (changeset docs/RFC-changeset-v0.3-to-v0.4.md).

Acceptance B4 (a transactional connector re-asserts the scope predicate inside
the effect's own transaction; a target reassigned between decision and dispatch
settles ``FAILED scope-lost`` and the effect never lands) and B5 (a window
connector's stale target is caught by the worker's pre-dispatch re-resolve, and
the connector's declared residual window is surfaced in the audit record).
Driven through the in-memory outbox + dispatch worker; the real-SQL transactional
path is exercised in ``test_m4_pg_integration.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from acp_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    PendingState,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from acp_core.connector import (
    SCOPE_LOST,
    ConnectorResult,
    ScopeCapability,
    ScopeReassertion,
    scope_capability_of,
)
from acp_core.models import ResolvedAction
from acp_core.scope import ScopePredicate, make_scope_resolver
from acp_connectors import InMemoryConnector
from acp_store import DispatchWorker, InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

ACTOR = Actor(id="alice", claims={"tenant": "T1"})


@dataclass
class Harness:
    reg: Any
    policy: Any
    audit: InMemoryAuditSink
    outbox: InMemoryOutboxStore
    conn: InMemoryConnector

    @property
    def connectors(self) -> Connectors:
        return Connectors({"sql": self.conn, "in_memory": self.conn, "email": self.conn})

    def enforce(self, resource: str, action: str, data: dict[str, Any]) -> Any:
        return enforce(
            RawCall(resource=resource, action=action, data=data),
            ACTOR,
            Session(id="s1", correlation_id="corr-1"),
            registry=self.reg,
            audit=self.audit,
            policy=self.policy,
            scopes=make_scope_resolver(self.policy),
            connectors=self.connectors,
            outbox=self.outbox,
        )

    def worker(self, *, scoped: bool = True) -> DispatchWorker:
        return DispatchWorker(
            self.outbox,
            self.connectors,
            registry=self.reg,
            scopes=make_scope_resolver(self.policy) if scoped else None,
        )

    def get(self, ticket: str) -> Any:
        row = self.outbox.get(ticket)
        assert row is not None
        return row


def harness(
    doc: dict[str, Any],
    tables: dict[str, list[dict[str, Any]]],
    *,
    capability: ScopeCapability | None = None,
) -> Harness:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    conn = InMemoryConnector(tables, scope_capability=capability)
    return Harness(reg, policy, audit, outbox, conn)


def _pay_doc() -> dict[str, Any]:
    return {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "scope": {"Payment": "tenantOf"},
    }


def _pay_tables() -> dict[str, list[dict[str, Any]]]:
    return {"Payment": [{"id": "P-1", "tenant_id": "T1"}]}


# --- B4: transactional connector — re-assert inside the effect's transaction
def test_b4_reassigned_target_settles_scope_lost() -> None:
    h = harness(_pay_doc(), _pay_tables())  # InMemoryConnector is transactional by default
    result = h.enforce("Payment", "pay", {"id": "P-1", "amount": 100})
    assert result.decision is Decision.ALLOW  # in scope at decision time

    # the race: the target moves to another tenant between decision and dispatch
    h.conn.tables["Payment"][0]["tenant_id"] = "T2"

    assert h.worker().drain() == 1
    row = h.get(result.ticket)
    assert row.state is PendingState.FAILED
    assert row.reason == SCOPE_LOST
    assert h.conn.effects == []  # the effect never landed on un-authorized state

    last = h.audit.records[-1]
    assert last.decision is Decision.DENY
    assert last.outcome == "failure"
    assert "Payment:tenantOf" in last.scopeApplied
    assert "reassertion:transactional" in last.scopeApplied


def test_b4_intact_scope_dispatches_and_audits_the_reassertion_form() -> None:
    h = harness(_pay_doc(), _pay_tables())
    result = h.enforce("Payment", "pay", {"id": "P-1", "amount": 100})

    assert h.worker().drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE
    assert len(h.conn.effects) == 1
    assert "reassertion:transactional" in h.audit.records[-1].scopeApplied


def test_scope_lost_never_stages_a_compensation() -> None:
    # A generic dispatch failure of an irreversible effect auto-stages its
    # compensation (design §9) — but scope-lost means the write did NOT happen
    # ("authorized state or not at all"), so there is nothing to compensate.
    doc = {
        "agent": "rx",
        "allow": [{"effect": ["prescribe"]}],
        "scope": {"Prescribing": "tenantOf"},
    }
    h = harness(doc, {"Prescribing": [{"id": "RX-1", "tenant_id": "T1"}]})
    result = h.enforce("Prescribing", "prescribe", {"id": "RX-1"})
    h.conn.tables["Prescribing"][0]["tenant_id"] = "T2"

    h.worker().drain()
    assert h.get(result.ticket).state is PendingState.FAILED
    assert h.get(result.ticket).reason == SCOPE_LOST
    assert h.outbox.list_by_state(PendingState.PENDING) == []  # no auto-staged undo


# --- B5: window connector — pre-dispatch re-resolve + declared window --------
WINDOW_CAP = ScopeCapability.window_declared("in-memory probe")


def test_b5_window_connector_catches_reassignment_pre_dispatch() -> None:
    h = harness(_pay_doc(), _pay_tables(), capability=WINDOW_CAP)
    result = h.enforce("Payment", "pay", {"id": "P-1", "amount": 100})
    h.conn.tables["Payment"][0]["tenant_id"] = "T2"

    assert h.worker().drain() == 1
    row = h.get(result.ticket)
    assert row.state is PendingState.FAILED
    assert row.reason == SCOPE_LOST
    assert h.conn.effects == []  # caught before the connector call — nothing sent

    last = h.audit.records[-1]
    assert last.decision is Decision.DENY
    assert "reassertion:window:in-memory probe" in last.scopeApplied


def test_b5_declared_window_surfaces_in_audit_on_success() -> None:
    h = harness(_pay_doc(), _pay_tables(), capability=WINDOW_CAP)
    result = h.enforce("Payment", "pay", {"id": "P-1", "amount": 100})

    assert h.worker().drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE
    # the residual risk is priced in the audit record, not hidden
    assert "reassertion:window:in-memory probe" in h.audit.records[-1].scopeApplied


# --- opt-in and fail-closed edges ------------------------------------------
def test_worker_without_scopes_preserves_v03_behaviour() -> None:
    # CS-018 is opt-in wiring: a worker with no scope resolver dispatches exactly
    # as v0.3 did — the decide→dispatch race stays the documented boundary.
    h = harness(_pay_doc(), _pay_tables())
    result = h.enforce("Payment", "pay", {"id": "P-1", "amount": 100})
    h.conn.tables["Payment"][0]["tenant_id"] = "T2"

    assert h.worker(scoped=False).drain() == 1
    assert h.get(result.ticket).state is PendingState.DONE


class _MisdeclaredConnector:
    """Declares transactional but cannot carry the predicate (no dispatch_scoped)."""

    scope_capability = ScopeCapability.transactional()

    def execute(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> ConnectorResult:
        return ConnectorResult(kind="receipt", receipt={"ok": True})

    def dispatch(self, action: ResolvedAction, actor: Actor, idempotency_key: str) -> ConnectorResult:
        return ConnectorResult(kind="receipt", receipt={"sent": True})

    def fetch_target(self, action: ResolvedAction, scope: ScopePredicate | None, actor: Actor) -> Mapping[str, Any] | None:
        return dict(action.data)

    def cancel(self, handle: str) -> None:
        return None


def test_transactional_declaration_without_dispatch_scoped_fails_closed() -> None:
    reg = full_registry()
    policy = load_policy(_pay_doc(), reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    bad = _MisdeclaredConnector()
    connectors = Connectors({"sql": bad})

    result = enforce(
        RawCall(resource="Payment", action="pay", data={"id": "P-1"}),
        ACTOR,
        Session(id="s1"),
        registry=reg, audit=audit, policy=policy,
        scopes=make_scope_resolver(policy), connectors=connectors, outbox=outbox,
    )
    assert result.decision is Decision.ALLOW

    worker = DispatchWorker(outbox, connectors, registry=reg, scopes=make_scope_resolver(policy))
    assert worker.drain() == 1
    assert result.ticket is not None
    row = outbox.get(result.ticket)
    assert row is not None and row.state is PendingState.FAILED
    assert row.reason == "scope-unavailable"  # cannot re-assert ⇒ never dispatched


def test_targetless_effect_has_nothing_to_reassert() -> None:
    # An effect that names no target row — the shape of an auto-staged
    # compensation, which bypasses the decision-time scope pre-check — has
    # nothing the predicate could select, so the transactional re-assert is
    # skipped and the effect dispatches normally.
    h = harness(_pay_doc(), _pay_tables())
    resolved = h.reg.resolve(RawCall(resource="Payment", action="refund", data={"amount": 10}))
    row = h.outbox.stage(
        resolved=resolved, actor=ACTOR, session_id="s1", agent="pay",
        state=PendingState.PENDING,
    )
    assert h.worker().drain() == 1
    assert h.get(row.id).state is PendingState.DONE
    assert len(h.conn.effects) == 1


# --- capability declaration unit checks --------------------------------------
def test_scope_capability_pairing_is_validated() -> None:
    with pytest.raises(ValueError):
        ScopeCapability(reassertion=ScopeReassertion.WINDOW)  # window must be declared
    with pytest.raises(ValueError):
        ScopeCapability(reassertion=ScopeReassertion.TRANSACTIONAL, window="none")


def test_undeclared_connector_is_priced_as_an_undeclared_window() -> None:
    cap = scope_capability_of(object())
    assert cap.reassertion is ScopeReassertion.WINDOW
    assert cap.window == "undeclared"
    assert cap.audit_note() == "reassertion:window:undeclared"


def test_shipped_connectors_declare_their_capability() -> None:
    from acp_connectors import EmailConnector, HttpConnector, SqlConnector

    assert SqlConnector(conn=None).scope_capability.reassertion is ScopeReassertion.TRANSACTIONAL
    assert InMemoryConnector().scope_capability.reassertion is ScopeReassertion.TRANSACTIONAL
    assert HttpConnector().scope_capability.reassertion is ScopeReassertion.WINDOW
    assert EmailConnector().scope_capability.reassertion is ScopeReassertion.WINDOW
