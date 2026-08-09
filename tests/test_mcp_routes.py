"""The MCP routes on the assembled app (design §1.2). Skipped without FastAPI.

``/mcp/tools`` and ``/mcp/search`` are how an agent learns what it may ask for;
``/mcp/call`` is the only way it acts. The route tests exist to pin the property
that matters: retrieval changes what the agent is *shown* and nothing about what
it is *allowed*, and the call path ends in the same ``enforce`` as every other
transport.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from stonefold_core import Connectors, InMemoryAuditSink, load_policy  # noqa: E402
from stonefold_core.registry import load_registry  # noqa: E402
from stonefold_connectors import InMemoryConnector  # noqa: E402
from stonefold_gates.engine import DefaultGateEngine  # noqa: E402
from stonefold_gateway.main import create_app  # noqa: E402
from stonefold_gateway.transport import Gateway  # noqa: E402
from stonefold_store import InMemoryOutboxStore  # noqa: E402
from tests.conftest import full_registry, load_schema  # noqa: E402

HEADERS = {"X-Actor-Id": "alice", "X-Session-Id": "s1"}

_REGISTRY_DOC = {
    "resources": {
        "Payment": {
            "actions": {
                "pay": {
                    "kind": "effect",
                    "label": "Pay a supplier invoice — the outbound bank payment.",
                    "data": {
                        "invoiceNo": {"type": "string", "required": True},
                        "channel": {"values": ["sepa", "swift"]},
                    },
                },
                "suspend": {
                    "kind": "effect",
                    "label": "Suspend a pending payment so it will not be disbursed.",
                    "data": {"invoiceNo": {"type": "string", "required": True}},
                },
                "read": {"kind": "observe", "label": "Read a payment record."},
            }
        }
    }
}


def _client(
    policy_doc: dict[str, Any], registry_doc: dict[str, Any] | None = None
) -> tuple[TestClient, InMemoryAuditSink]:
    reg = load_registry(registry_doc) if registry_doc else full_registry()
    audit = InMemoryAuditSink()
    gateway = Gateway(
        registry=reg, audit=audit,
        policy=load_policy(policy_doc, reg, schema=load_schema()),
        gates=DefaultGateEngine(reg), outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"email": InMemoryConnector(), "sql": InMemoryConnector(),
                               "in_memory": InMemoryConnector()}),
    )
    return TestClient(create_app(gateway)), audit


_PAY = {"agent": "pay", "allow": [{"effect": ["pay"]}]}
_READ_ONLY = {"agent": "reader", "allow": [{"observe": ["read"]}]}


# --- discovery -------------------------------------------------------------
def test_tools_endpoint_serves_the_whole_surface() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.get("/mcp/tools").json()
    assert {t["name"] for t in body["tools"]} == {
        "Payment.pay", "Payment.suspend", "Payment.read"
    }
    assert body["of"] == 3
    pay = next(t for t in body["tools"] if t["name"] == "Payment.pay")
    assert pay["input_schema"]["required"] == ["invoiceNo"]


def test_search_endpoint_narrows_the_surface() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.get(
        "/mcp/search", params={"q": "suspend the pending payment", "limit": 1}
    ).json()
    assert body["of"] == 3
    assert [r["name"] for r in body["results"]] == ["Payment.suspend"]


def test_a_pure_paraphrase_can_rank_the_wrong_action_first() -> None:
    """The limitation, pinned rather than described.

    Matching is lexical: "stop a payment going out" shares no word with
    ``suspend`` or its description, while "payment"/"out" both hit
    ``Payment.pay`` ("the outbound bank payment"). So the wrong action ranks
    first, and the right one is still in the list — which is why the endpoint
    returns candidates rather than an answer, and why the gate, not the ranking,
    is what stops the payment.
    """
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.get("/mcp/search", params={"q": "stop a payment going out"}).json()
    names = [r["name"] for r in body["results"]]
    assert names[0] == "Payment.pay"
    assert "Payment.suspend" in names


def test_search_shows_actions_policy_would_refuse() -> None:
    """Retrieval is not authorization, and this is the check that says so.

    A read-only agent still *sees* ``Payment.pay`` — the surface is the registry,
    not the policy — and is refused when it calls it. Filtering the catalogue by
    policy would move an enforcement decision into a ranking function.
    """
    client, _ = _client(_READ_ONLY, _REGISTRY_DOC)
    body = client.get("/mcp/search", params={"q": "pay a supplier invoice"}).json()
    assert "Payment.pay" in [r["name"] for r in body["results"]]

    refused = client.post(
        "/mcp/call",
        json={"tool": "Payment.pay", "arguments": {"invoiceNo": "INV-1"}},
        headers=HEADERS,
    ).json()
    assert refused["decision"] == "deny"


# --- the call path ---------------------------------------------------------
def test_call_ends_in_the_same_enforcement() -> None:
    client, audit = _client(_PAY, _REGISTRY_DOC)
    body = client.post(
        "/mcp/call",
        json={"tool": "Payment.pay", "arguments": {"invoiceNo": "INV-7", "channel": "sepa"}},
        headers=HEADERS,
    ).json()
    assert body["decision"] == "allow"
    # audited like any other transport, under the actor from the header
    assert [r.resource for r in audit.records] == ["Payment"]
    assert audit.records[0].actor == "alice"


def test_an_unmapped_tool_is_denied_not_passed_through() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.post(
        "/mcp/call", json={"tool": "shell.exec", "arguments": {"cmd": "rm -rf /"}},
        headers=HEADERS,
    ).json()
    assert body["decision"] == "deny"


def test_a_malformed_call_is_a_shape_error_not_a_refusal() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.post(
        "/mcp/call", json={"tool": "Payment.pay", "arguments": {"channel": "sepa"}},
        headers=HEADERS,
    ).json()
    assert body["decision"] == "error"
    assert body["reasonCode"] == "MISSING_FIELD"
    assert body["error"]["pointer"] == "data.invoiceNo"


def test_identity_comes_from_the_transport() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    missing = client.post(
        "/mcp/call", json={"tool": "Payment.pay", "arguments": {"invoiceNo": "INV-1"}}
    )
    assert missing.status_code == 422  # no X-Actor-Id: the body cannot supply one


# --- the intent endpoint reports shape errors the same way ----------------
def test_submit_intent_reports_the_same_shape_error() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.post(
        "/submit_intent",
        json={"resource": "Payment", "action": "pay", "data": {"invoiceNo": "INV-1",
                                                              "nope": 1}},
        headers=HEADERS,
    ).json()
    assert body["decision"] == "error"
    assert body["error"]["pointer"] == "data.nope"


def test_a_batch_shape_error_points_at_the_operation() -> None:
    client, _ = _client(_PAY, _REGISTRY_DOC)
    body = client.post(
        "/submit_intent",
        json={"operations": [
            {"resource": "Payment", "action": "pay", "data": {"invoiceNo": "INV-1"}},
            {"resource": "Payment", "action": "pay", "data": {}},
        ]},
        headers=HEADERS,
    ).json()
    assert body["decision"] == "error"
    assert body["error"]["pointer"] == "operations[1].data.invoiceNo"
