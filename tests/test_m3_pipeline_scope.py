"""M3 — scope + execution wired through the pipeline (RFC §6.3/§12, design §5).

Acceptance B1 (read scope injected below the model — in-memory analogue of the
SQL test), B2 (scope-on-effect is a pre-resolution DENY), B3 (the actor cannot set
its own scope), plus observe/record/transition executing end to end.
"""

from __future__ import annotations

from typing import Any

from acp_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
    make_scope_resolver,
)
from acp_connectors import EmailConnector, HttpConnector, InMemoryConnector
from tests.conftest import full_registry, load_schema

CUSTOMERS = {
    "Customer": [
        {"id": 1, "owner_id": "alice", "name": "a-one"},
        {"id": 2, "owner_id": "alice", "name": "a-two"},
        {"id": 3, "owner_id": "alice", "name": "a-three"},
        {"id": 4, "owner_id": "bob", "name": "b-one"},
        {"id": 5, "owner_id": "carol", "name": "c-one"},
    ]
}
PAYMENTS = {
    "Payment": [
        {"id": "a1", "tenant_id": "T1"},
        {"id": "a2", "tenant_id": "T2"},
    ]
}


def _run(
    doc: dict[str, Any],
    resource: str,
    action: str,
    actor: Actor,
    *,
    data: dict[str, Any] | None = None,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    connectors: Connectors | None = None,
) -> Any:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    mem = InMemoryConnector(tables=tables or {})
    conns = connectors or Connectors(
        {"sql": mem, "in_memory": mem, "email": EmailConnector(), "http": HttpConnector()}
    )
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource=resource, action=action, data=data or {}),
        actor,
        Session(id="s1"),
        registry=reg,
        audit=audit,
        policy=policy,
        scopes=make_scope_resolver(policy),
        connectors=conns,
    )
    return result, audit, mem


# --- B1: read scope injected below the model -----------------------------
def test_b1_read_scope_returns_only_owned_rows() -> None:
    doc = {
        "agent": "support",
        "allow": [{"observe": ["Customer"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
    }
    # the agent asks for "all" — scope still narrows to alice's three rows.
    result, audit, _ = _run(
        doc, "Customer", "read", Actor(id="alice"), data={"q": "all"}, tables=CUSTOMERS
    )
    assert result.decision is Decision.ALLOW
    assert len(result.output) == 3
    assert {r["owner_id"] for r in result.output} == {"alice"}
    assert audit.records[-1].scopeApplied == ["Customer:assignedToCurrentUser"]


def test_b1_different_actor_sees_their_own_scope() -> None:
    doc = {
        "agent": "support",
        "allow": [{"observe": ["Customer"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
    }
    result, _, _ = _run(doc, "Customer", "read", Actor(id="bob"), tables=CUSTOMERS)
    assert {r["id"] for r in result.output} == {4}


# --- B3: the actor cannot set its own scope ------------------------------
def test_b3_payload_owner_id_is_ignored() -> None:
    doc = {
        "agent": "support",
        "allow": [{"observe": ["Customer"]}],
        "scope": {"Customer": "assignedToCurrentUser"},
    }
    # a prompt-injected payload tries to widen scope by naming owner_id.
    result, _, _ = _run(
        doc, "Customer", "read", Actor(id="alice"),
        data={"owner_id": "carol", "q": "all"}, tables=CUSTOMERS,
    )
    # identity comes from the session actor, not the payload — still alice's rows.
    assert {r["owner_id"] for r in result.output} == {"alice"}
    assert len(result.output) == 3


# --- B2: scope on an effect is a pre-resolution authorization check -------
def test_b2_effect_on_out_of_scope_target_is_denied() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "scope": {"Payment": "tenantOf"},
    }
    alice_t1 = Actor(id="alice", claims={"tenant": "T1"})
    denied, audit, mem = _run(
        doc, "Payment", "pay", alice_t1, data={"id": "a2"}, tables=PAYMENTS
    )
    assert denied.decision is Decision.DENY
    assert denied.rule == "scope-denied"
    assert mem.effects == []  # never dispatched


def test_b2_effect_on_in_scope_target_is_allowed() -> None:
    doc = {
        "agent": "pay",
        "allow": [{"effect": ["pay"]}],
        "scope": {"Payment": "tenantOf"},
    }
    alice_t1 = Actor(id="alice", claims={"tenant": "T1"})
    allowed, _, _ = _run(doc, "Payment", "pay", alice_t1, data={"id": "a1"}, tables=PAYMENTS)
    # in-scope target passes the pre-resolution check; effect staging is M4.
    assert allowed.decision is Decision.ALLOW


# --- observe / record / transition execute end to end --------------------
def test_record_executes_and_appends() -> None:
    doc = {"agent": "support", "allow": [{"record": ["Note"]}]}
    result, _, mem = _run(doc, "Note", "create", Actor(id="alice"), data={"text": "call back"})
    assert result.decision is Decision.ALLOW
    assert result.output == {"created": True, "resource": "Note"}
    assert mem.tables["Note"] == [{"text": "call back"}]


def test_transition_executes() -> None:
    doc = {"agent": "support", "allow": [{"transition": {"Order": ["confirm"]}}]}
    tables = {"Order": [{"id": "o1", "state": "pending_confirmation"}]}
    result, _, mem = _run(
        doc, "Order", "confirm", Actor(id="alice"), data={"id": "o1"}, tables=tables
    )
    assert result.decision is Decision.ALLOW
    assert result.output["transitioned"] is True
    assert mem.tables["Order"][0]["state"] == "confirm"


# --- fail closed when the connector is missing (invariant 7) --------------
def test_missing_connector_fails_closed() -> None:
    doc = {"agent": "support", "allow": [{"observe": ["Customer"]}]}
    result, _, _ = _run(
        doc, "Customer", "read", Actor(id="alice"),
        tables=CUSTOMERS, connectors=Connectors({}),
    )
    assert result.decision is Decision.DENY
    assert result.rule == "connector-unavailable"
