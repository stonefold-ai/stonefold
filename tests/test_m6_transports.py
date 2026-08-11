"""M6 — transports and interception coverage (design §0, §1; spec §3).

Both transports must drive the *same* pipeline (the chokepoint, design §0): the
``submit_intent`` endpoint and the MCP proxy each end in the identical ``enforce``
verdict. Plus the interception guards: an unmapped tool denies, a
free-form pass-through needs acknowledgement, and a stray (non-gateway) tool
endpoint fails the startup coverage check.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from stonefold_core.enums import Coverage, EnforcementMode
from stonefold_core.pipeline import ADVISORY_RULE
from stonefold_gateway.transport import (
    UNMAPPED_TOOL,
    UPSTREAM_UNAVAILABLE,
    CoverageError,
    Gateway,
    MCPProxy,
    SifNativeTransport,
    ToolMapping,
    interception_coverage_check,
    mcp_tool_schemas,
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


# --- the surface is generated from the registry (design §1.2) -------------
def test_mcp_tools_cover_exactly_the_declared_actions() -> None:
    reg = full_registry()
    tools = {t["name"] for t in mcp_tool_schemas(reg)}
    assert "Email.sendEmail" in tools
    # nothing beyond the registry: an agent cannot be offered an undeclared action
    assert tools == {
        f"{resource}.{action}"
        for resource, rdef in reg.file.resources.items()
        for action in rdef.actions
    }


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


# --- the coverage half: what an advisory deployment cannot judge ----------
# Under enforcement an unmapped tool is denied and the architecture is done.
# An advisory deployment refuses nothing but a kill order, so the same tool is
# forwarded to the endpoint it was going to and recorded as never judged — the
# part of the report that says how much of the estate a policy could see.
_ADVISORY_DOC: dict[str, Any] = {
    "agent": "support",
    "allow": [{"effect": ["sendEmail"]}],
    "defaults": {"enforcement": "advisory"},
}


def _advisory_gateway(audit: InMemoryAuditSink) -> Gateway:
    reg = full_registry()
    policy = load_policy(
        _ADVISORY_DOC, reg, schema=load_schema(), advisory_permitted=True
    )
    return Gateway(
        registry=reg, audit=audit, policy=policy, gates=DefaultGateEngine(reg),
        outbox=InMemoryOutboxStore(audit=audit),
        connectors=Connectors({"email": InMemoryConnector(), "sql": InMemoryConnector(),
                               "in_memory": InMemoryConnector()}),
    )


class _RecordingUpstream:
    """The tool endpoint the proxy sits in front of."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def __call__(self, tool: str, args: Mapping[str, Any]) -> Any:
        self.calls.append((tool, dict(args)))
        if self._fail:
            raise RuntimeError("the real MCP server is down")
        return {"rows": 1}


def test_advisory_forwards_an_unmapped_tool_and_records_it_unjudged() -> None:
    audit = InMemoryAuditSink()
    upstream = _RecordingUpstream()
    proxy = MCPProxy(
        _advisory_gateway(audit),
        [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")],
        upstream=upstream,
    )

    result = proxy.call_tool(
        "run_sql", {"q": "select 1"}, actor=ALICE, session=SESSION
    )

    # it reached the endpoint it was going to, and the agent got its answer
    assert upstream.calls == [("run_sql", {"q": "select 1"})]
    assert result.decision is Decision.ALLOW
    assert result.output == {"rows": 1}

    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.coverage is Coverage.UNJUDGED  # never counted as an allow
    assert record.resource == "run_sql"
    assert record.outcome == "success"  # it happened
    # what an enforcing deployment would have done, kept as the reflex it is
    assert record.advised is not None
    assert record.advised.decision is Decision.DENY
    assert record.advised.rule == UNMAPPED_TOOL
    assert record.rule == UNMAPPED_TOOL  # spec §11: the deciding rule


def test_the_agent_is_not_told_its_tool_went_unjudged() -> None:
    """An agent that learns which of its tools are ungoverned is no longer
    producing the traffic being measured."""
    audit = InMemoryAuditSink()
    proxy = MCPProxy(
        _advisory_gateway(audit),
        [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")],
        upstream=_RecordingUpstream(),
    )

    result = proxy.call_tool("run_sql", {"q": "select 1"}, actor=ALICE, session=SESSION)

    assert result.rule == ADVISORY_RULE
    assert UNMAPPED_TOOL not in str(result.rule)
    assert result.reason_code == ""
    assert not hasattr(result, "coverage")


def test_a_forward_that_fails_is_the_estates_outage_not_a_verdict() -> None:
    """D-A3: where the failure is what prevents the action, the action fails —
    the caller sees what it would have seen without us, and the record says the
    gateway never judged it."""
    audit = InMemoryAuditSink()
    proxy = MCPProxy(
        _advisory_gateway(audit),
        [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")],
        upstream=_RecordingUpstream(fail=True),
    )

    with pytest.raises(RuntimeError):
        proxy.call_tool("run_sql", {"q": "select 1"}, actor=ALICE, session=SESSION)

    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.coverage is Coverage.UNJUDGED
    assert record.decision is Decision.DENY  # it did not happen
    assert record.rule == UPSTREAM_UNAVAILABLE
    assert record.outcome == "failure"


def test_advisory_without_a_forwarder_still_refuses_and_says_so() -> None:
    """§5's other half: the gateway cannot let through what it cannot address,
    so the action fails — and the record still carries the mode."""
    audit = InMemoryAuditSink()
    proxy = MCPProxy(
        _advisory_gateway(audit),
        [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")],
    )

    result = proxy.call_tool("run_sql", {"q": "select 1"}, actor=ALICE, session=SESSION)

    assert result.decision is Decision.DENY
    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ADVISORY
    assert record.coverage is Coverage.UNJUDGED
    assert record.advised is None  # nothing diverged: it was refused for real


def test_a_forwarder_on_an_enforcing_gateway_fails_startup() -> None:
    """The bypass must not be sitting in the configuration when the pilot
    converts — that is exactly when nobody re-reads the proxy's arguments."""
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)

    with pytest.raises(CoverageError):
        MCPProxy(
            gw,
            [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")],
            upstream=_RecordingUpstream(),
        )


def test_an_enforcing_gateway_refuses_to_forward_even_if_asked() -> None:
    """The check lives in the class that owns the mode, not only in the caller
    that knows about it today."""
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)
    upstream = _RecordingUpstream()

    with pytest.raises(CoverageError):
        gw.forward_unjudged(
            tool="run_sql", args={"q": "select 1"}, actor=ALICE, session=SESSION,
            upstream=upstream,
        )
    assert upstream.calls == []  # nothing reached the endpoint
    assert audit.records == []


def test_an_enforcing_unmapped_refusal_is_still_marked_unjudged() -> None:
    """Coverage is about whether the gateway could judge the call, not about the
    mode: a structural refusal never judged anything in either deployment."""
    audit = InMemoryAuditSink()
    gw = _gateway({"agent": "support", "allow": [{"effect": ["sendEmail"]}]}, audit)
    proxy = MCPProxy(gw, [ToolMapping(tool="send_mail", resource="Email", action="sendEmail")])

    proxy.call_tool("run_sql", {"q": "select 1"}, actor=ALICE, session=SESSION)

    record = audit.records[-1]
    assert record.enforcement is EnforcementMode.ENFORCED
    assert record.coverage is Coverage.UNJUDGED
