"""M5 — the operator kill REST surface (plan M5 task 4). Smoke-tests the three
endpoints over a real ``KillService`` + in-memory store; the deeper kill
semantics are covered by the E1–E5 suites. Skipped if FastAPI is unavailable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from stonefold_core import Actor, InMemoryAuditSink, RawCall, Session  # noqa: E402
from stonefold_core.kill import KillTarget  # noqa: E402
from stonefold_store.kill_memory import InMemoryKillStore  # noqa: E402
from stonefold_gateway.kill_api import create_kill_router  # noqa: E402
from stonefold_gateway.kill_service import KillService  # noqa: E402
from tests.conftest import full_registry  # noqa: E402


def _client(store: InMemoryKillStore, audit: InMemoryAuditSink) -> TestClient:
    service = KillService(store, audit=audit)
    app = FastAPI()
    app.include_router(create_kill_router(service))
    return TestClient(app)


def _target(session: str = "s1") -> KillTarget:
    resolved = full_registry().resolve(RawCall(resource="Email", action="sendEmail",
                                               data={"to": "x@acme.example"}))
    return KillTarget.from_resolved(resolved, Actor(id="alice"), Session(id=session), "support")


def test_issue_session_kill_via_rest() -> None:
    store = InMemoryKillStore()
    audit = InMemoryAuditSink()
    client = _client(store, audit)

    resp = client.post("/kill", json={"scope": "session", "session_id": "s1",
                                      "issued_by": "alice@ops"})
    assert resp.status_code == 200
    order_id = resp.json()["id"]
    assert order_id.startswith("kill_")

    # the kill is now live in the store
    assert store.matches(_target("s1")) is not None
    assert any(r.action == "kill.issue" for r in audit.records)


def test_list_and_lift_via_rest() -> None:
    store = InMemoryKillStore()
    audit = InMemoryAuditSink()
    client = _client(store, audit)

    issued = client.post("/kill", json={"scope": "global", "issued_by": "op"}).json()
    assert len(client.get("/kill").json()) == 1

    lifted = client.post(f"/kill/{issued['id']}/lift", json={"lifted_by": "op2"})
    assert lifted.status_code == 200
    assert client.get("/kill").json() == []  # nothing active after the lift
    assert store.matches(_target()) is None


def test_action_class_kill_requires_a_facet() -> None:
    client = _client(InMemoryKillStore(), InMemoryAuditSink())
    resp = client.post("/kill", json={"scope": "action_class", "issued_by": "op"})
    assert resp.status_code == 422


def test_lift_unknown_order_is_404() -> None:
    client = _client(InMemoryKillStore(), InMemoryAuditSink())
    resp = client.post("/kill/kill_does_not_exist/lift", json={"lifted_by": "op"})
    assert resp.status_code == 404
