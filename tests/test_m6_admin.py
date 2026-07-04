"""M6 — the admin console and app factory (plan M6 task 4, DoD).

Drives the assembled FastAPI app end to end: a trace replays a run (intent →
decision → effect), the approvals inbox lets a human release a held action
(dual-auth rejects self-approval), the kill button halts a session's next action,
and ``submit_intent`` takes identity from the transport header — never the body
(invariant 3). Skipped if FastAPI is unavailable.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from stonefold_core import Connectors, InMemoryAuditSink, load_policy  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_gates.engine import DefaultGateEngine  # noqa: E402
from stonefold_gateway.kill_service import KillService  # noqa: E402
from stonefold_gateway.main import create_app  # noqa: E402
from stonefold_gateway.transport import Gateway  # noqa: E402
from stonefold_store import InMemoryOutboxStore  # noqa: E402
from stonefold_store.kill_memory import InMemoryKillStore  # noqa: E402
from tests.conftest import full_registry, load_schema


def _app(doc: dict[str, Any]) -> tuple[TestClient, InMemoryAuditSink, Any, InMemoryKillStore]:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    audit = InMemoryAuditSink()
    outbox = InMemoryOutboxStore(audit=audit)
    kill = InMemoryKillStore()
    gateway = Gateway(
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        outbox=outbox, kill=kill,
        connectors=Connectors({"email": InMemoryConnector(), "sql": InMemoryConnector(),
                               "in_memory": InMemoryConnector()}),
    )
    app = create_app(gateway, kill_service=KillService(kill, audit=audit),
                     audit=audit, outbox=outbox)
    return TestClient(app), audit, outbox, kill


def _headers(actor: str = "alice", session: str = "s1", corr: str | None = None) -> dict[str, str]:
    h = {"X-Actor-Id": actor, "X-Session-Id": session}
    if corr:
        h["X-Correlation-Id"] = corr
    return h


_SUPPORT = {"agent": "support",
            "allow": [{"observe": ["read"]}, {"effect": ["sendEmail"]}]}
_APPROVAL = {"agent": "pay", "allow": [{"effect": ["pay"]}],
             "gates": {"pay": {"requireApproval": {"approvers": "role:finance"}}}}
_DUAL = {"agent": "pay", "allow": [{"effect": ["pay"]}],
         "gates": {"pay": {"dualAuthorization": {"approvers": "role:treasury"}}}}


# --- the tool schema is served --------------------------------------------
def test_tool_schema_endpoint() -> None:
    client, *_ = _app(_SUPPORT)
    schema = client.get("/tool-schema").json()
    assert schema["name"] == "submit_intent"
    assert "Email" in schema["parameters"]["properties"]["resource"]["enum"]


# --- trace: a run replays as intent → decision → effect -------------------
def test_trace_shows_a_run() -> None:
    client, _audit, _outbox, _kill = _app(_SUPPORT)
    h = _headers(corr="run-1")
    client.post("/submit_intent", json={"resource": "Customer", "action": "read"}, headers=h)
    client.post("/submit_intent",
                json={"resource": "Email", "action": "sendEmail", "data": {"to": "x@acme.example"}},
                headers=h)

    trace = client.get("/admin/trace/run-1").json()
    assert [r["resource"] for r in trace] == ["Customer", "Email"]
    assert {r["decision"] for r in trace} == {"allow"}
    assert any(r["outcome"] == "staged" for r in trace)  # the effect was staged


# --- approvals inbox: a human releases a held action ----------------------
def test_approvals_inbox_and_approve() -> None:
    client, _audit, outbox, _kill = _app(_APPROVAL)
    res = client.post("/submit_intent",
                      json={"resource": "Payment", "action": "pay", "data": {"amount": 1}},
                      headers=_headers()).json()
    assert res["decision"] == "hold"
    ticket = res["ticket"]

    inbox = client.get("/admin/approvals").json()
    assert [a["id"] for a in inbox] == [ticket]
    assert inbox[0]["state"] == "pending_approval"

    approved = client.post(f"/admin/approvals/{ticket}/approve", json={"approver": "boss"}).json()
    assert approved["state"] == "pending"  # released for dispatch


def test_dual_auth_rejects_self_approval() -> None:
    client, _audit, _outbox, _kill = _app(_DUAL)
    res = client.post("/submit_intent",
                      json={"resource": "Payment", "action": "pay", "data": {"amount": 1}},
                      headers=_headers(actor="alice")).json()
    ticket = res["ticket"]
    # the actor cannot approve her own dual-auth action (design §7)
    self_approve = client.post(f"/admin/approvals/{ticket}/approve", json={"approver": "alice"})
    assert self_approve.status_code == 409


def test_reject_unknown_ticket_is_404() -> None:
    client, *_ = _app(_APPROVAL)
    resp = client.post("/admin/approvals/act_nope/reject", json={"approver": "boss"})
    assert resp.status_code == 404


# --- kill button halts the session's next action --------------------------
def test_kill_button_halts_next_action() -> None:
    client, _audit, _outbox, _kill = _app(_SUPPORT)
    killed = client.post("/kill", json={"scope": "session", "session_id": "s-kill",
                                        "issued_by": "console"})
    assert killed.status_code == 200

    res = client.post("/submit_intent",
                      json={"resource": "Email", "action": "sendEmail", "data": {"to": "x@acme.example"}},
                      headers=_headers(session="s-kill")).json()
    assert res["decision"] == "halt"  # distinct from deny

    # a different session is unaffected
    ok = client.post("/submit_intent",
                     json={"resource": "Email", "action": "sendEmail", "data": {"to": "x@acme.example"}},
                     headers=_headers(session="s-live")).json()
    assert ok["decision"] == "allow"


# --- invariant 3: identity from the transport, not the body ---------------
def test_identity_comes_from_header_not_body() -> None:
    client, _audit, _outbox, _kill = _app(_SUPPORT)
    # the body smuggles actor/owner_id; they must be ignored as identity
    client.post(
        "/submit_intent",
        json={"resource": "Email", "action": "sendEmail",
              "data": {"to": "x@acme.example", "actor": "evil", "owner_id": "tenant-2"}},
        headers=_headers(actor="alice", corr="run-id"),
    )
    trace = client.get("/admin/trace/run-id").json()
    assert len(trace) == 1
    record = trace[0]
    assert record["actor"] == "alice"               # identity from the header
    assert record["parameters"]["actor"] == "evil"  # the smuggled value is opaque data only
