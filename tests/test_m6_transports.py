"""M6 — transports and interception coverage (design §0, §1; RFC §3).

Both transports must drive the *same* pipeline (the chokepoint, design §0): the
SIF-native ``submit_intent`` tool and the MCP proxy each end in the identical
``enforce`` verdict. Plus the interception guards: an unmapped tool denies, a
free-form pass-through needs acknowledgement, and a stray (non-gateway) tool
endpoint fails the startup coverage check.
"""

from __future__ import annotations

from typing import Any

import pytest

from stonefold_core import (
    Actor,
    Connectors,
    Decision,
    InMemoryAuditSink,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from stonefold_connectors import InMemoryConnector
from stonefold_gates.engine import DefaultGateEngine
from stonefold_gateway.transport import (
    CoverageError,
    Gateway,
    MCPProxy,
    SifNativeTransport,
    ToolMapping,
    interception_coverage_check,
    submit_intent_schema,
)
from stonefold_store import InMemoryOutboxStore
from tests.conftest import full_registry, load_schema

ALICE = Actor(id="alice")
SESSION = Session(id="s1", correlation_id="run-T")


def _gateway(doc: dict[str, Any], audit: InMemoryAuditSink) -> Gateway:
    reg = full_registry()
    policy = load_policy(doc, reg, schema=load_schema())
    return Gateway(
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"email": InMemoryConnector(), "sql": InMemoryConnector(),
                               "in_memory": InMemoryConnector()}),
    )


# --- the schema is generated from the registry (design §1.1) --------------
def test_submit_intent_schema_lists_registry_actions() -> None:
    schema = submit_intent_schema(full_registry())
    assert schema["name"] == "submit_intent"
    enum = schema["parameters"]["properties"]["resource"]["enum"]
    assert "Customer" in enum and "Email" in enum
    assert schema["x-acp-actions"]["Email"] == ["sendEmail"]
    # the single-tool property: the agent gets exactly one tool name.
    assert schema["parameters"]["additionalProperties"] is False


# --- both transports drive the SAME pipeline ------------------------------
def test_submit_intent_matches_direct_enforce() -> None:
    audit = InMemoryAuditSink()
    doc = {"agent": "support", "allow": [{"effect": ["sendEmail"]}]}
    gw = _gateway(doc, audit)
    sif = SifNativeTransport(gw)

    via_tool = sif.submit_intent(
        {"resource": "Email", "action": "sendEmail", "data": {"to": "x@acme.example"}},
        actor=ALICE, session=SESSION,
    )
    assert via_tool.decision is Decision.ALLOW

    # the same intent enforced directly yields the same decision
    reg = full_registry()
    direct = enforce(
        RawCall(resource="Email", action="sendEmail", data={"to": "x@acme.example"}),
        ALICE, SESSION, registry=reg, audit=InMemoryAuditSink(),
        policy=load_policy(doc, reg, schema=load_schema()), gates=DefaultGateEngine(reg),
        outbox=InMemoryOutboxStore(), connectors=Connectors({"email": InMemoryConnector()}),
    )
    assert via_tool.decision is direct.decision


def test_submit_intent_denies_out_of_policy_action() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"observe": ["read"]}]}, audit)
    result = SifNativeTransport(gw).submit_intent(
        {"resource": "Email", "action": "sendEmail", "data": {"to": "x@acme.example"}},
        actor=ALICE, session=SESSION,
    )
    assert result.decision is Decision.DENY


# --- MCP proxy mapping + coverage (design §1.2) ---------------------------
def test_mapped_tool_routes_through_gateway() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)
    proxy = MCPProxy(gw, [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")])

    result = proxy.call_tool("send_mail", {"to": "x@acme.example"}, actor=ALICE, session=SESSION)
    assert result.decision is Decision.ALLOW


def test_unmapped_tool_denies_and_is_audited() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)
    proxy = MCPProxy(gw, [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")])

    result = proxy.call_tool("run_sql", {"q": "DROP TABLE users"}, actor=ALICE, session=SESSION)
    assert result.decision is Decision.DENY
    assert result.rule == "unmapped-tool"
    # the refusal is recorded (never a silent pass-through)
    denials = [r for r in audit.records if r.decision is Decision.DENY and r.resource == "run_sql"]
    assert len(denials) == 1


def test_arg_map_renames_tool_args() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)
    mapping = ToolMapping(tool="send_mail", resource="Email", action="sendEmail",
                          arg_map={"recipient": "to"})
    assert mapping.to_data({"recipient": "x@acme.example"}) == {"to": "x@acme.example"}
    result = MCPProxy(gw, [mapping]).call_tool(
        "send_mail", {"recipient": "x@acme.example"}, actor=ALICE, session=SESSION)
    assert result.decision is Decision.ALLOW


def test_freeform_passthrough_requires_acknowledgement() -> None:
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"observe": ["read"]}]}, audit)
    raw = ToolMapping(tool="run_sql", resource="Customer", action="read", free_form=True)

    with pytest.raises(CoverageError):
        MCPProxy(gw, [raw])  # unacknowledged high-risk pass-through ⇒ refuses to start

    # explicit acknowledgement lets it start
    proxy = MCPProxy(gw, [raw], acknowledge_freeform=True)
    assert proxy is not None


# --- startup coverage check (design §1.2, review note) -------------------
def test_coverage_check_rejects_stray_endpoint() -> None:
    with pytest.raises(CoverageError):
        interception_coverage_check(
            ["https://gw.internal", "https://raw-mcp.evil"],
            gateway_endpoint="https://gw.internal",
        )


def test_coverage_check_passes_when_all_route_through_gateway() -> None:
    # no raise: every configured endpoint is the gateway
    interception_coverage_check(
        ["https://gw.internal", "https://gw.internal"],
        gateway_endpoint="https://gw.internal",
    )
