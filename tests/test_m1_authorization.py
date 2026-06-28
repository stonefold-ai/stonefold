"""M1 — authorization (RFC §6.2, design §3/§4). Acceptance A1, A2, A3."""

from __future__ import annotations

from typing import Any

from acp_core import (
    Actor,
    CompiledPolicy,
    Decision,
    InMemoryAuditSink,
    Kind,
    KindMatcher,
    MatchSpecificity,
    RawCall,
    Session,
    enforce,
    load_policy,
)
from tests.conftest import full_registry, load_schema


def _compiled(policy: dict[str, Any]) -> CompiledPolicy:
    return load_policy(policy, full_registry(), schema=load_schema())


def _enforce(policy: CompiledPolicy, resource: str, action: str) -> Decision:
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource=resource, action=action),
        Actor(id="alice"),
        Session(id="s1"),
        registry=full_registry(),
        audit=audit,
        policy=policy,
    )
    # exactly one audit record per evaluation (RFC §11)
    assert len(audit.records) == 1
    assert audit.records[0].decision is result.decision
    return result.decision


# --- A1: default deny ---
def test_a1_default_deny() -> None:
    policy = _compiled({"agent": "a1", "allow": [{"observe": ["Customer"]}]})
    assert _enforce(policy, "Email", "sendEmail") is Decision.DENY
    # the allowed read is permitted by authorization
    assert _enforce(policy, "Customer", "read") is Decision.ALLOW


def test_a1_default_deny_rule_label() -> None:
    policy = _compiled({"agent": "a1", "allow": [{"observe": ["Customer"]}]})
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource="Email", action="sendEmail"),
        Actor(id="alice"),
        Session(id="s"),
        registry=full_registry(),
        audit=audit,
        policy=policy,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "default-deny"


# --- A2: deny overrides allow ---
def test_a2_deny_overrides_allow() -> None:
    policy = _compiled(
        {
            "agent": "a2",
            "allow": [{"effect": ["refund"]}],
            "deny": [{"effect": ["refund"]}],
        }
    )
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource="Payment", action="refund"),
        Actor(id="alice"),
        Session(id="s"),
        registry=full_registry(),
        audit=audit,
        policy=policy,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "deny-rule"  # deny wins even though allow matches


# --- A3: most-specific allow selects gates (action + kind, AND-combined) ---
def test_a3_gate_keys_action_and_kind() -> None:
    policy = _compiled(
        {
            "agent": "a3",
            "allow": [{"effect": ["sendEmail"]}],
            "gates": {
                "sendEmail": {"rate": "20/hour"},
                "effect": {"spendLimit": "25/session"},
            },
        }
    )
    reg = full_registry()
    resolved = reg.resolve(RawCall(resource="Email", action="sendEmail"))
    keys = policy.gate_keys_for(resolved)
    assert keys == ["sendEmail", "effect"]  # most-specific first
    merged = policy.gates_for(resolved)
    assert set(merged) == {"rate", "spendLimit"}  # AND-combined


# --- specificity ranking of the matcher (RFC §6.2 rule 4) ---
def test_matcher_specificity_ranking() -> None:
    m = KindMatcher()
    m.add(Kind.EFFECT, "*")
    m.add(Kind.EFFECT, ["sendEmail"])
    m.add(Kind.OBSERVE, ["Customer"])
    m.add(Kind.TRANSITION, {"Order": ["confirm"]})
    # named action beats the kind-level '*'
    assert m.match(Kind.EFFECT, "Email", "sendEmail") is MatchSpecificity.ACTION
    # an unlisted effect still matches via '*' (STAR)
    assert m.match(Kind.EFFECT, "Payment", "pay") is MatchSpecificity.STAR
    # bare resource grant
    assert m.match(Kind.OBSERVE, "Customer", "read") is MatchSpecificity.RESOURCE
    # {Resource: [action]} map grant is action-specific
    assert m.match(Kind.TRANSITION, "Order", "confirm") is MatchSpecificity.ACTION
    # no match
    assert m.match(Kind.OBSERVE, "Payment", "read") is None


def test_bare_resource_grants_all_actions_of_kind() -> None:
    policy = _compiled({"agent": "br", "allow": [{"observe": ["Patient"]}]})
    # both observe actions on Patient are granted by the bare resource grant
    assert _enforce(policy, "Patient", "read") is Decision.ALLOW
    assert _enforce(policy, "Patient", "readSealed") is Decision.ALLOW
    # but a non-observe action on Patient is not
    assert _enforce(policy, "Patient", "administer") is Decision.DENY


def test_unknown_action_denies_under_policy() -> None:
    policy = _compiled({"agent": "u", "allow": [{"observe": ["Customer"]}]})
    audit = InMemoryAuditSink()
    result = enforce(
        RawCall(resource="Ghost", action="boo"),
        Actor(id="alice"),
        Session(id="s"),
        registry=full_registry(),
        audit=audit,
        policy=policy,
    )
    assert result.decision is Decision.DENY
    assert result.rule == "unknown-action"
