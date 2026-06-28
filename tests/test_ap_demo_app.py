"""HTTP/WebSocket surface of the demo gateway (docs/05 UI backend).

Drives ``create_app`` with an in-process bundle through FastAPI's TestClient: the
SIF tool, header-based identity (invariant 3), the inbox, the in-process agent
runner (fake provider), the live-trace WebSocket, approvals, kill, and audit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from acp_ap_demo.app import create_app  # noqa: E402
from acp_ap_demo.gateway import build_inmemory_bundle  # noqa: E402

DEMO_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
_HDRS = {"X-Actor-Id": "ap-operator", "X-Session-Id": "http-1"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(build_inmemory_bundle(clock=lambda: DEMO_NOW), default_provider="fake")
    with TestClient(app) as c:
        yield c


def test_tool_schema_is_sif_native(client: TestClient) -> None:
    schema = client.get("/tool-schema").json()
    assert schema["name"] == "submit_intent"
    assert "Payment" in schema["parameters"]["properties"]["resource"]["enum"]


def test_inbox_lists_the_invoices(client: TestClient) -> None:
    invoices = client.get("/inbox").json()["invoices"]
    ids = {i["id"] for i in invoices}
    assert ids == {"INV-1001", "INV-1002", "INV-1003"}  # the agent's input feed (not gated)


def test_submit_intent_uses_header_identity(client: TestClient) -> None:
    body = {"resource": "Payment", "action": "pay",
            "data": {"payeeId": "PE-ACME-SUP", "accountId": "ACME-OPS", "amount": 800.0,
                     "currency": "USD", "destinationCountry": "GB"}}
    r = client.post("/submit_intent", json=body, headers=_HDRS)
    assert r.json()["decision"] == "allow"


def test_submit_intent_requires_identity_header(client: TestClient) -> None:
    r = client.post("/submit_intent", json={"resource": "Account", "action": "read"})
    assert r.status_code == 422  # missing X-Actor-Id / X-Session-Id


def test_agent_run_happy(client: TestClient) -> None:
    r = client.post("/agent/run", json={"scenario": "happy", "provider": "fake"}).json()
    assert any(d["decision"] == "allow" for d in r["decisions"])
    # the response carries the raw inputs the UI shows
    assert r["prompt"] and r["system"] and r["steps"]
    audit = client.get("/audit").json()
    assert any(a["action"] == "pay" and a["decision"] == "allow" for a in audit)


def test_agent_run_inbox_allows_holds_and_denies(client: TestClient) -> None:
    r = client.post("/agent/run", json={"scenario": "inbox", "provider": "fake"}).json()
    decs = {d["decision"] for d in r["decisions"]}
    # small allowed, mid-size held for approval, sanctioned vendor denied
    assert {"allow", "hold", "deny"} <= decs


def test_agent_run_blocked_is_denied(client: TestClient) -> None:
    r = client.post("/agent/run", json={"scenario": "blocked", "provider": "fake"}).json()
    assert any(d["decision"] == "deny" for d in r["decisions"])


def test_agent_run_gateway_off_bypasses(client: TestClient) -> None:
    # the UI toggle's "OFF" path: payments execute directly, nothing held
    r = client.post("/agent/run",
                    json={"scenario": "inbox", "mode": "unsafe", "provider": "fake"}).json()
    assert all(d.get("executed") for d in r["decisions"])
    assert all(d.get("decision") == "bypassed" for d in r["decisions"])


def test_approval_flow_over_http(client: TestClient) -> None:
    client.post("/agent/run", json={"scenario": "approval", "provider": "fake"})
    pending = client.get("/admin/approvals").json()
    assert len(pending) == 1
    out = client.post(f"/admin/approvals/{pending[0]['id']}/approve",
                      json={"approver": "mgr-1"}).json()
    assert out["state"] in ("pending", "done")
    assert client.get("/admin/approvals").json() == []


def test_trace_websocket_backfills(client: TestClient) -> None:
    # produce a decision first, then connect — the WS backfills recent history
    client.post("/submit_intent",
                json={"resource": "Account", "action": "read"}, headers=_HDRS)
    with client.websocket_connect("/ws/trace") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "decision"
        assert ev["resource"] == "Account"
